"""Remote alignment worker (``BOOKBRIDGE_MODE=worker``).

Takes books from a main BookBridge instance's alignment queue and aligns them
here, with this machine's CPUs: claim a book under a lease, download the inputs
the main instance resolved, run the ordinary alignment pipeline
(``SyncManager._run_background_job``) against a scratch database, and upload the
map it stored. The main instance publishes it through its own regression veto.

A worker starts no web server, KoSync server, sync cycle, socket listener or
sync client. It never opens the main instance's database and never holds its
``secret.key`` or any reader's credentials; its only state is a scratch
directory it deletes after every book. See ``src/services/alignment_workers.py``
for the main instance's side.

Environment:
    WORKER_MAIN_URL       the main instance's dashboard URL (port 5757, not KOSYNC_PORT)
    WORKER_TOKEN          the main instance's ALIGNMENT_WORKER_TOKEN
    WORKER_NAME           shown on the main instance's Job Queue page (default: hostname)
    WORKER_LEASE_SECONDS  lease length; heartbeats every third of it (default 300)
    WORKER_POLL_SECONDS   wait between claims while the queue is empty (default 30)
    DATA_DIR              model cache and scratch space (default /data)
    plus the engine settings: CTC_MODEL, CTC_DEVICE, ALIGN_THREADS, WHISPER_*, ...
"""

from __future__ import annotations

import gzip
import hashlib
import inspect
import json
import logging
import os
import shutil
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

if __package__ in (None, ""):  # run as `python src/alignment_worker.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

logger = logging.getLogger("bookbridge.alignment_worker")

_CHUNK = 1024 * 1024


class LeaseLost(Exception):
    """The main instance ended this worker's lease; abandon the book."""


class MainClient:
    """HTTP client for the main instance's /api/v1/worker routes."""

    def __init__(self, base_url: str, token: str, timeout: float = 60):
        self.base = base_url.rstrip("/") + "/api/v1/worker"
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {token}"
        self.timeout = timeout

    def _check(self, response: requests.Response) -> requests.Response:
        if response.status_code == 409:
            raise LeaseLost(response.text[:200])
        response.raise_for_status()
        return response

    def claim(self, worker: str, lease_seconds: int) -> Optional[dict]:
        response = self.session.post(f"{self.base}/claim", json={
            "worker": worker, "lease_seconds": lease_seconds,
        }, timeout=self.timeout)
        if response.status_code == 204:
            return None
        return self._check(response).json()

    def inputs(self, lease_id: str) -> Optional[dict]:
        """The manifest, or None while the main instance is still preparing it."""
        response = self.session.get(f"{self.base}/jobs/{lease_id}/inputs",
                                    headers={"Accept-Encoding": "gzip"}, timeout=self.timeout)
        if response.status_code == 202:
            return None
        return self._check(response).json()

    def heartbeat(self, lease_id: str, progress: float, lease_seconds: int) -> None:
        self._check(self.session.post(f"{self.base}/jobs/{lease_id}/heartbeat", json={
            "progress": progress, "lease_seconds": lease_seconds,
        }, timeout=self.timeout))

    def download(self, lease_id: str, entry: dict, dest: Path, lost: threading.Event) -> None:
        """Download one input file, resuming a partial one, and verify it."""
        expected_size, expected_sha = int(entry["size"]), entry["sha256"]
        for attempt in range(1, 4):
            have = dest.stat().st_size if dest.exists() else 0
            if have > expected_size:
                dest.unlink()
                have = 0
            if have < expected_size:
                headers = {"Range": f"bytes={have}-"} if have else {}
                with self.session.get(f"{self.base}/jobs/{lease_id}/files/{entry['index']}",
                                      headers=headers, stream=True, timeout=self.timeout) as response:
                    self._check(response)
                    mode = "ab" if have and response.status_code == 206 else "wb"
                    with open(dest, mode) as out:
                        for chunk in response.iter_content(_CHUNK):
                            if lost.is_set():
                                raise LeaseLost("lease ended during download")
                            out.write(chunk)
            if dest.stat().st_size == expected_size and _sha256(dest) == expected_sha:
                return
            logger.warning("⚠️ Input file %s failed verification (attempt %s); downloading again",
                           entry["index"], attempt)
            dest.unlink(missing_ok=True)
        raise RuntimeError(f"input file {entry['index']} failed verification three times")

    def result(self, lease_id: str, payload: dict) -> dict:
        body = gzip.compress(json.dumps(payload).encode("utf-8"), compresslevel=6)
        response = self.session.post(
            f"{self.base}/jobs/{lease_id}/result", data=body,
            headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
            timeout=max(self.timeout, 300),
        )
        if response.status_code == 422:
            raise ResultRefused(response.json().get("error", response.text[:500]))
        return self._check(response).json()

    def failure(self, lease_id: str, error_class: str, message: str) -> None:
        self._check(self.session.post(f"{self.base}/jobs/{lease_id}/failure", json={
            "error_class": error_class, "message": message,
        }, timeout=self.timeout))


class ResultRefused(Exception):
    """The main instance refused the uploaded result (reported back as a failure)."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Heartbeat(threading.Thread):
    """Keeps a lease alive and reports progress; cancels the job once the lease is gone."""

    def __init__(self, client: MainClient, lease_id: str, lease_seconds: int,
                 progress: Callable[[], float], on_lost: Callable[[], None]):
        super().__init__(daemon=True, name="alignment-worker-heartbeat")
        self.client, self.lease_id, self.lease_seconds = client, lease_id, lease_seconds
        self.progress, self.on_lost = progress, on_lost
        self.lost = threading.Event()
        self._halt = threading.Event()

    def run(self) -> None:
        interval = max(5.0, self.lease_seconds / 3)
        while not self._halt.wait(interval):
            try:
                self.client.heartbeat(self.lease_id, self.progress(), self.lease_seconds)
            except LeaseLost:
                logger.warning("🛑 The main instance ended this lease; abandoning the book")
                self.lost.set()
                self.on_lost()
                return
            except requests.RequestException as e:
                # The lease outlives a few missed beats; keep trying.
                logger.warning("⚠️ Heartbeat failed: %s", e)

    def stop(self) -> None:
        self._halt.set()


class WorkerAudioSource:
    """Audio source adapter over the downloaded parts (registered under the book's source name)."""

    def __init__(self, audio_paths: list[Path], extensions: list[str], chapters: list):
        self._files = [{"local_path": str(p), "ext": ext} for p, ext in zip(audio_paths, extensions)]
        self._chapters = chapters

    def get_audio_files(self, source_id: str, bridge_key: str | None = None) -> list[dict]:
        return [dict(entry) for entry in self._files]

    def get_chapters(self, source_id: str) -> list:
        return list(self._chapters)


class WorkerLibrary:
    """Library service that 'acquires' the downloaded EPUB."""

    def __init__(self, epub_path: Path):
        self.epub_path = epub_path

    def acquire_ebook(self, item_details, book) -> str:
        return str(self.epub_path)


def _record_publishes(alignment_service) -> dict:
    """Wrap ``_publish_map`` to remember the last map the pipeline actually stored."""
    recorded: dict[str, Any] = {}
    original = alignment_service._publish_map
    signature = inspect.signature(original)

    def publish(*args, **kwargs):
        stored = original(*args, **kwargs)
        if stored:
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            recorded["last"] = dict(bound.arguments)
        return stored

    alignment_service._publish_map = publish
    return recorded


class AlignmentWorker:
    def __init__(self, client: MainClient, name: str, data_dir: Path,
                 lease_seconds: int = 300, poll_seconds: float = 30):
        self.client, self.name = client, name
        self.data_dir = data_dir
        self.scratch_root = data_dir / "worker_scratch"
        self.lease_seconds, self.poll_seconds = lease_seconds, poll_seconds
        self._base_env = dict(os.environ)

    def run_forever(self) -> None:
        shutil.rmtree(self.scratch_root, ignore_errors=True)
        backoff = self.poll_seconds
        logger.info("🛠️ Alignment worker '%s' polling %s", self.name, self.client.base)
        while True:
            try:
                claimed = self.client.claim(self.name, self.lease_seconds)
                backoff = self.poll_seconds
            except requests.RequestException as e:
                logger.warning("⚠️ Could not reach the main instance: %s", e)
                time.sleep(backoff)
                backoff = min(backoff * 2, 300)
                continue
            if claimed is None:
                time.sleep(self.poll_seconds)
                continue
            self.run_job(claimed)

    def run_job(self, claimed: dict) -> None:
        lease_id = claimed["lease_id"]
        lease_seconds = int(claimed.get("lease_seconds") or self.lease_seconds)
        job_dir = self.scratch_root / lease_id
        job_dir.mkdir(parents=True, exist_ok=True)
        logger.info("📥 Took '%s' (lease %s)", claimed.get("title"), lease_id[:8])

        from src.utils.transcription_cancel import register_worker, unregister_worker

        abs_id = claimed["abs_id"]
        token = register_worker(abs_id)
        progress = {"value": 0.0, "db": None}

        def current_progress() -> float:
            db = progress["db"]
            if db is not None:
                from src.db.models import JOB_KIND_ALIGNMENT
                job = db.get_latest_job(abs_id, kind=JOB_KIND_ALIGNMENT)
                if job is not None and job.progress is not None:
                    progress["value"] = float(job.progress)
            return progress["value"]

        heartbeat = Heartbeat(self.client, lease_id, lease_seconds, current_progress, token.cancel)
        heartbeat.start()
        try:
            manifest = self._wait_for_inputs(lease_id, heartbeat.lost)
            payload = self._align(manifest, job_dir, token, heartbeat.lost, progress)
            if heartbeat.lost.is_set() or token.is_cancelled():
                raise LeaseLost("cancelled")
            try:
                outcome = self.client.result(lease_id, payload)
                logger.info("✅ '%s' uploaded (%s)", claimed.get("title"), outcome.get("status"))
            except ResultRefused as refused:
                logger.error("❌ The main instance refused the result: %s", refused)
                self.client.failure(lease_id, "ResultRefused", f"Result refused: {refused}")
        except LeaseLost:
            logger.info("🛑 Abandoned '%s'", claimed.get("title"))
        except Exception as e:
            if heartbeat.lost.is_set():
                logger.info("🛑 Abandoned '%s'", claimed.get("title"))
            else:
                logger.error("❌ '%s' failed: %s", claimed.get("title"), e, exc_info=True)
                try:
                    self.client.failure(lease_id, type(e).__name__, str(e))
                except (LeaseLost, requests.RequestException) as report_err:
                    logger.warning("⚠️ Could not report the failure: %s", report_err)
        finally:
            heartbeat.stop()
            unregister_worker(abs_id, token)
            db = progress["db"]
            if db is not None:
                db.db_manager.close()
            shutil.rmtree(job_dir, ignore_errors=True)

    def _wait_for_inputs(self, lease_id: str, lost: threading.Event) -> dict:
        while True:
            if lost.is_set():
                raise LeaseLost("lease ended while inputs were prepared")
            manifest = self.client.inputs(lease_id)
            if manifest is not None:
                return manifest
            time.sleep(5)

    def _apply_settings(self, settings: dict) -> None:
        """The main instance's alignment settings for this job, over the worker's own env."""
        from src.services.alignment_workers import PIPELINE_SETTING_KEYS

        for key in PIPELINE_SETTING_KEYS:
            if key in self._base_env:
                os.environ[key] = self._base_env[key]
            else:
                os.environ.pop(key, None)
        for key, value in (settings or {}).items():
            if key in PIPELINE_SETTING_KEYS and isinstance(value, str):
                os.environ[key] = value

    def _align(self, manifest: dict, job_dir: Path, token, lost: threading.Event,
               progress: dict) -> dict:
        from src.db.database_service import DatabaseService
        from src.db.models import JOB_KIND_ALIGNMENT, Book, BookAlignment, Job
        from src.services.alignment_service import AlignmentService
        from src.services.alignment_workers import result_from_publish
        from src.sync_manager import SyncManager
        from src.utils.ebook_utils import EbookParser
        from src.utils.polisher import Polisher
        from src.utils.smil_extractor import SmilExtractor
        from src.utils.transcriber import AudioTranscriber
        from src.api.llm_client import create_llm_client

        lease_id = manifest["lease_id"]
        files_dir = job_dir / "files"
        files_dir.mkdir(parents=True, exist_ok=True)
        local_paths: list[Path] = []
        for entry in manifest["files"]:
            suffix = ".epub" if entry["role"] == "epub" else f".{entry.get('ext') or 'mp3'}"
            dest = files_dir / f"{entry['index']:03d}{suffix}"
            self.client.download(lease_id, entry, dest, lost)
            local_paths.append(dest)
        epub_name = manifest["files"][0].get("name") or "book.epub"
        epub_path = files_dir / Path(epub_name).name
        local_paths[0].rename(epub_path)
        audio_entries = manifest["files"][1:]

        self._apply_settings(manifest.get("settings") or {})
        work_dir = job_dir / "work"
        work_dir.mkdir(parents=True, exist_ok=True)
        db = DatabaseService(str(job_dir / "scratch.db"))
        llm = create_llm_client()
        polisher = Polisher()
        ebook_parser = EbookParser(str(files_dir), epub_cache_dir=work_dir / "epub_cache", ollama_client=llm)
        alignment_service = AlignmentService(db, polisher, ollama_client=llm)
        transcriber = AudioTranscriber(work_dir, SmilExtractor(), polisher, ollama_client=llm)
        published = _record_publishes(alignment_service)

        book_fields = manifest["book"]
        audio_source = book_fields.get("audio_source") or "ABS"
        adapter = WorkerAudioSource(
            local_paths[1:], [e.get("ext") or "mp3" for e in audio_entries], manifest.get("chapters") or [],
        )
        manager = SyncManager(
            transcriber=transcriber, ebook_parser=ebook_parser, database_service=db,
            alignment_service=alignment_service, sync_clients={},
            audio_source_adapters={audio_source: adapter},
            epub_cache_dir=work_dir / "epub_cache", data_dir=work_dir, books_dir=files_dir,
        )

        abs_id = book_fields["abs_id"]
        book = Book(abs_id=abs_id)
        for field, value in book_fields.items():
            setattr(book, field, value)
        book.audio_source = audio_source
        book.status = "processing"
        db.save_book(book)
        db.save_job(Job(abs_id=abs_id, last_attempt=time.time(), retry_count=0, progress=0.0,
                        kind=JOB_KIND_ALIGNMENT))
        existing = manifest.get("existing_alignment")
        if existing:
            with db.get_session() as session:
                session.add(BookAlignment(
                    abs_id=abs_id,
                    alignment_map_json=json.dumps(existing["alignment_map"]),
                    align_method=existing.get("align_method"),
                    total_chars=existing.get("total_chars"),
                    segments_json=(json.dumps(existing["segments"])
                                   if existing.get("segments") is not None else None),
                ))
        progress["db"] = db

        manager._run_background_job(
            db.get_book(abs_id), 1, 1, library_service=WorkerLibrary(epub_path),
            client_bundle=None, cancellation_token=token, raise_failures=True,
        )
        if lost.is_set() or token.is_cancelled():
            raise LeaseLost("cancelled")
        done = db.get_book(abs_id)
        if done is None or done.status != "active":
            raise RuntimeError("the alignment pipeline ended without storing a map")
        payload = result_from_publish(published.get("last"), done, done.transcript_source)
        payload["epub_sha256"] = _sha256(epub_path)
        return payload


def main() -> int:
    # The app's own console/file logging (configured when the module is imported);
    # a basicConfig here as well would print every line twice.
    import src.utils.logging_utils  # noqa: F401
    main_url = os.environ.get("WORKER_MAIN_URL", "").strip()
    token = os.environ.get("WORKER_TOKEN", "")
    if not main_url or not token:
        logger.error("❌ Worker mode needs WORKER_MAIN_URL and WORKER_TOKEN")
        return 2
    name = os.environ.get("WORKER_NAME", "").strip() or socket.gethostname()
    worker = AlignmentWorker(
        MainClient(main_url, token),
        name=name,
        data_dir=Path(os.environ.get("DATA_DIR", "/data")),
        lease_seconds=int(os.environ.get("WORKER_LEASE_SECONDS", "300")),
        poll_seconds=float(os.environ.get("WORKER_POLL_SECONDS", "30")),
    )
    worker.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
