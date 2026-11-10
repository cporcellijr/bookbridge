"""Remote alignment workers: the main instance's side.

Alignment runs one book at a time in the main instance's background thread
(``SyncManager.check_pending_jobs``). With ``ALIGNMENT_WORKER_TOKEN`` set, the
main instance also hands books from the same queue to remote workers (the same
image started with ``BOOKBRIDGE_MODE=worker``, see ``src/alignment_worker.py``)
through the ``/api/v1/worker`` blueprint (``src/api/alignment_worker_api.py``).

The division of work keeps every decision on the main instance:

* **Claim.** A worker claims through ``DatabaseService.claim_next_alignment``,
  the claim the local thread uses, so the Job Queue order holds for everyone.
  The claim carries a lease; heartbeats extend it, and an expired lease puts
  the book back in the queue at its old position, counted as an attempt.
* **Inputs.** The main instance resolves the EPUB and audio exactly as a local
  job does (the claimant's library service and audio source), locks the KOSync
  id, and serves the files. A worker never holds library or reader credentials.
* **Results.** The worker uploads its candidate map; the main instance stores it
  through ``AlignmentService._publish_map`` (regression veto and backup row) and
  finishes the book with ``SyncManager._finish_alignment_success``. Failures go
  through ``SyncManager._record_alignment_failure``, the local job's own retry
  logic.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Optional

from src.services.alignment_service import ContentMatchRejected
from src.services.library_service import EbookInputError
from src.services.segment_fit import Segment
from src.utils.logging_utils import sanitize_log_data
from src.utils.transcriber import TranscriptionCancelled

logger = logging.getLogger(__name__)

MIN_TOKEN_LENGTH = 32
DEFAULT_LEASE_SECONDS = 300
MIN_LEASE_SECONDS = 60
MAX_LEASE_SECONDS = 3600

# Settings that decide how a book is aligned (rather than which engine runs it),
# sent with each job so a worker aligns the way this instance would. Engine and
# hardware settings (CTC_MODEL, CTC_DEVICE, ALIGN_THREADS, WHISPER_*, the
# transcription provider and its keys, the LLM client) are the worker's own.
PIPELINE_SETTING_KEYS = (
    "CTC_ENABLED",
    "SMIL_SKIP_CTC",
    "ALIGNMENT_SEGMENTED_MAPS",
    "CONTENT_MATCH_GUARD",
    "CONTENT_MATCH_MIN_OVERLAP",
    "SMIL_VALIDATION_THRESHOLD",
    "TRANSCRIPT_MATCH_THRESHOLD",
    "TRANSCRIPT_MIN_COVERAGE",
    "AUDIO_SPLIT_DURATION_MINUTES",
)

# Book fields the alignment pipeline reads; the worker's scratch copy of the book.
BOOK_FIELDS = (
    "abs_id", "abs_title", "audio_source", "audio_source_id", "audio_duration",
    "duration", "ebook_filename", "original_ebook_filename", "kosync_doc_id",
    "sync_mode", "transcript_source", "series_name", "series_sequence",
)

_RECOVERY_STAGES = frozenset({"ebook_input_recovery", "ebook_input_recovery_manual"})
_FAILURE_CLASSES = {
    "ContentMatchRejected": ContentMatchRejected,
    "EbookInputError": EbookInputError,
}

_short_token_warned = False
_local_flag_warned = False


def worker_token() -> Optional[str]:
    """ALIGNMENT_WORKER_TOKEN when it is long enough to enable the worker API."""
    global _short_token_warned
    token = os.environ.get("ALIGNMENT_WORKER_TOKEN", "")
    if not token:
        return None
    if len(token) < MIN_TOKEN_LENGTH:
        if not _short_token_warned:
            logger.warning(
                f"⚠️ ALIGNMENT_WORKER_TOKEN is shorter than {MIN_TOKEN_LENGTH} characters; "
                "remote alignment workers stay disabled"
            )
            _short_token_warned = True
        return None
    return token


def local_worker_enabled() -> bool:
    """Whether the main instance aligns books remote workers could take.

    ALIGNMENT_LOCAL_WORKER=false hands those books to workers only. It needs the
    worker API: without a valid token no worker could take them, so the local
    thread keeps aligning everything (with a warning).
    """
    global _local_flag_warned
    value = os.environ.get("ALIGNMENT_LOCAL_WORKER", "true").strip().lower()
    if value not in ("false", "0", "no", "off"):
        return True
    if worker_token() is None:
        if not _local_flag_warned:
            logger.warning(
                "⚠️ ALIGNMENT_LOCAL_WORKER=false needs ALIGNMENT_WORKER_TOKEN; "
                "aligning locally until remote workers are enabled"
            )
            _local_flag_warned = True
        return True
    return False


def remote_eligible(book, latest_job) -> bool:
    """Whether a remote worker may take this book.

    Workers are offered full-pipeline books only. Audio-only and ebook-only books
    have no alignment work. Storyteller-linked books align from Storyteller's
    transcripts on this instance's disk (cheap, and not something to ship), and
    ebook input recovery needs this instance's retained caches.
    """
    if getattr(book, "sync_mode", None) not in (None, "audiobook"):
        return False
    if getattr(book, "transcript_source", None) == "storyteller" or getattr(book, "storyteller_uuid", None):
        return False
    if getattr(latest_job, "stage", None) in _RECOVERY_STAGES:
        return False
    return True


def clamp_lease_seconds(value: Any) -> int:
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        seconds = DEFAULT_LEASE_SECONDS
    return max(MIN_LEASE_SECONDS, min(MAX_LEASE_SECONDS, seconds))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class LeaseGone(Exception):
    """The lease is no longer current: the worker must abandon the book."""


class AlignmentWorkerCoordinator:
    """Serves the alignment queue to remote workers (one per main instance)."""

    def __init__(self, sync_manager, database_service):
        self.sync_manager = sync_manager
        self.database_service = database_service
        self._preparing: dict[str, threading.Thread] = {}
        self._preparing_lock = threading.Lock()

    # --- claim / heartbeat ---------------------------------------------------

    def claim(self, worker_name: str, lease_seconds: Any = None) -> Optional[dict]:
        """Lease the next remote-eligible book to ``worker_name``, or None."""
        max_retries = int(os.getenv("JOB_MAX_RETRIES", 5))
        retry_delay_mins = int(os.getenv("JOB_RETRY_DELAY_MINS", 15))
        lease_seconds = clamp_lease_seconds(lease_seconds)
        self.sync_manager._expire_alignment_leases(max_retries)
        claim = self.database_service.claim_next_alignment(
            eligible=remote_eligible,
            owner=worker_name,
            lease_seconds=lease_seconds,
            max_retries=max_retries,
            retry_delay_mins=retry_delay_mins,
        )
        if claim is None:
            return None
        lease_id = claim.lease_id
        book = claim.book
        logger.info(
            "📋 Alignment queue: worker '%s' took '%s' (%s of %s; retry=%s)",
            sanitize_log_data(worker_name), sanitize_log_data(book.abs_title),
            claim.job_idx, claim.job_total, claim.is_retry,
        )
        self._start_prepare(lease_id)
        return {
            "lease_id": lease_id,
            "abs_id": book.abs_id,
            "title": book.abs_title,
            "lease_seconds": lease_seconds,
        }

    def heartbeat(self, lease_id: str, progress: Any = None, lease_seconds: Any = None) -> bool:
        """Extend a lease by ``lease_seconds`` from now and record the worker's
        progress; False when the lease is gone."""
        try:
            progress = None if progress is None else float(progress)
        except (TypeError, ValueError):
            progress = None
        return self.database_service.renew_alignment_lease(
            lease_id, clamp_lease_seconds(lease_seconds), progress=progress,
        )

    # --- inputs ----------------------------------------------------------------

    def inputs(self, lease_id: str) -> tuple[str, Optional[dict]]:
        """("gone", None), ("preparing", None) or ("ready", manifest) for a lease."""
        lease = self.database_service.get_alignment_lease(lease_id)
        if lease is None:
            return "gone", None
        job, book = lease
        if not job.lease_inputs_json:
            self._start_prepare(lease_id)
            return "preparing", None
        stored = json.loads(job.lease_inputs_json)
        manifest = dict(stored["manifest"])
        manifest["existing_alignment"] = self._existing_alignment(book.abs_id)
        return "ready", manifest

    def file_path(self, lease_id: str, index: int) -> Path:
        """Local path of the lease's input file ``index``; LeaseGone / IndexError / FileNotFoundError."""
        lease = self.database_service.get_alignment_lease(lease_id)
        if lease is None:
            raise LeaseGone(lease_id)
        job, _book = lease
        if not job.lease_inputs_json:
            raise FileNotFoundError("inputs are still being prepared")
        paths = json.loads(job.lease_inputs_json)["paths"]
        if index < 0 or index >= len(paths):
            raise IndexError(index)
        path = Path(paths[index])
        if not path.is_file():
            raise FileNotFoundError(str(path))
        return path

    def _start_prepare(self, lease_id: str) -> None:
        with self._preparing_lock:
            running = self._preparing.get(lease_id)
            if running is not None and running.is_alive():
                return
            thread = threading.Thread(
                target=self._prepare, args=(lease_id,), daemon=True,
                name=f"alignment-worker-inputs-{lease_id[:8]}",
            )
            self._preparing[lease_id] = thread
            thread.start()

    def _prepare(self, lease_id: str) -> None:
        """Resolve a leased book's inputs exactly as a local job would."""
        lease = self.database_service.get_alignment_lease(lease_id)
        if lease is None:
            return
        _job, book = lease
        sm = self.sync_manager
        client_bundle, library_service = sm._claimant_for_book(book)
        try:
            with sm._claimant_context(client_bundle, library_service):
                stored = self._resolve_inputs(lease_id, book, library_service)
            with self.database_service._alignment_lease_lock:
                if self.database_service.get_alignment_lease(lease_id) is None:
                    return
                self.database_service.set_alignment_lease_inputs(lease_id, json.dumps(stored))
            logger.info(
                "📦 Alignment worker inputs ready for '%s' (%s files)",
                sanitize_log_data(book.abs_title), len(stored["paths"]),
            )
        except (TranscriptionCancelled, LeaseGone):
            logger.info("🛑 Alignment worker inputs for '%s' abandoned: lease ended", book.abs_id)
        except Exception as e:
            logger.error(
                "❌ %s: could not prepare alignment worker inputs: %s",
                sanitize_log_data(book.abs_title), e, exc_info=True,
            )
            with self.database_service._alignment_lease_lock:
                lease = self.database_service.get_alignment_lease(lease_id)
                if lease is not None:
                    sm._record_alignment_failure(lease[1], e, client_bundle)
        finally:
            with self._preparing_lock:
                self._preparing.pop(lease_id, None)

    def _persist_for(self, lease_id: str, book):
        """``persist_book`` for main-side work on a leased book: stops once the lease ends."""
        def persist_book() -> None:
            if self.database_service.get_alignment_lease(lease_id) is None:
                raise LeaseGone(lease_id)
            if self.database_service.update_book_if_exists(book) is None:
                raise TranscriptionCancelled(book.abs_id)
        return persist_book

    def _resolve_inputs(self, lease_id: str, book, library_service) -> dict:
        sm = self.sync_manager
        abs_id = book.abs_id
        persist_book = self._persist_for(lease_id, book)
        audio_source = sm._get_audio_source_name(book)
        audio_adapter = sm._get_audio_source_adapter(book)
        audio_source_id = getattr(book, "audio_source_id", None) or abs_id
        abs_client = sm.active_abs_client

        item_details = None
        if audio_source == "ABS" and abs_client:
            item_details = abs_client.get_item_details(abs_id)
        sm._backfill_series_from_item(book, item_details, persist_book)

        epub_path = sm._resolve_alignment_epub(book, item_details, library_service)
        if not epub_path:
            raise FileNotFoundError(f"Could not locate or download: {book.ebook_filename}")
        if epub_path.suffix.lower() != '.epub':
            raise EbookInputError(f"Ebook input refused: {epub_path.name} is not an EPUB. Choose the correct ebook and retry.")
        book_text, _spine = sm.ebook_parser.extract_text_and_map(epub_path)
        if not book_text.strip():
            raise EbookInputError(f"Ebook input refused: {epub_path.name} has no readable EPUB text. Replace the file and retry.")
        sm._lock_kosync_id(book, epub_path, persist_book)

        if not audio_adapter:
            raise RuntimeError(f"No audio source adapter configured for '{audio_source}'")
        chapters = audio_adapter.get_chapters(audio_source_id) or []
        audio_files = audio_adapter.get_audio_files(audio_source_id, bridge_key=abs_id) or []
        if not audio_files:
            raise RuntimeError(f"No audio files found for '{abs_id}'")
        audio_paths = [self._local_audio_file(abs_id, idx, entry, abs_client)
                       for idx, entry in enumerate(audio_files)]

        paths = [str(epub_path)] + [str(p) for p in audio_paths]
        files = [{
            "index": 0, "role": "epub", "name": epub_path.name,
            "size": epub_path.stat().st_size, "sha256": _sha256(epub_path),
        }]
        for idx, (path, entry) in enumerate(zip(audio_paths, audio_files), start=1):
            files.append({
                "index": idx, "role": "audio",
                "ext": str(entry.get("ext") or path.suffix.lstrip(".") or "mp3"),
                "size": path.stat().st_size, "sha256": _sha256(path),
            })
        settings = {key: os.environ[key] for key in PIPELINE_SETTING_KEYS if key in os.environ}
        manifest = {
            "lease_id": lease_id,
            "book": {field: getattr(book, field, None) for field in BOOK_FIELDS},
            "chapters": chapters,
            "settings": settings,
            "files": files,
        }
        return {"paths": paths, "manifest": manifest}

    @staticmethod
    def _local_audio_file(abs_id: str, idx: int, entry: dict, abs_client) -> Path:
        """An audio part as a local file, downloading a stream-only ABS part when needed."""
        local = entry.get("local_path")
        if local and os.path.isfile(local):
            return Path(local)
        stream_url = entry.get("stream_url")
        if not stream_url or abs_client is None:
            raise RuntimeError(f"Audio part {idx} of '{abs_id}' is not available as a file")
        data_dir = Path(os.environ.get("DATA_DIR", "/data"))
        ext = str(entry.get("ext") or "mp3").lower().lstrip(".")
        if not ext.isalnum():
            ext = "mp3"
        target = data_dir / "audio_cache" / str(abs_id).replace(":", "_") / "source_tracks" / f"track_{idx:03d}.{ext}"
        if not target.is_file() or target.stat().st_size == 0:
            target.parent.mkdir(parents=True, exist_ok=True)
            if not abs_client.download_file(stream_url, target):
                raise RuntimeError(f"Audio part {idx} of '{abs_id}' could not be downloaded")
        return target

    def _existing_alignment(self, abs_id: str) -> Optional[dict]:
        """The stored map a Remap chunks CTC against, as the worker needs it."""
        service = self.sync_manager.alignment_service
        alignment_map = service._get_alignment(abs_id)
        if not alignment_map:
            return None
        return {
            "alignment_map": alignment_map,
            "align_method": self.database_service.get_alignment_method(abs_id),
            "total_chars": service._get_alignment_total_chars(abs_id),
            "segments": service._get_segments(abs_id),
        }

    # --- results -----------------------------------------------------------

    def complete(self, lease_id: str, result: dict) -> dict:
        """Publish a worker's result on this instance. Raises LeaseGone / ValueError."""
        sm = self.sync_manager
        with self.database_service._alignment_lease_lock:
            lease = self.database_service.get_alignment_lease(lease_id)
            if lease is None:
                raise LeaseGone(lease_id)
            job, book = lease
            if not job.lease_inputs_json:
                raise ValueError("result for a lease whose inputs were never prepared")
            stored = json.loads(job.lease_inputs_json)
            epub_path = Path(stored["paths"][0])
            worker = job.lease_owner or "unknown worker"

            # The KOSync id was locked from the served file when the inputs were
            # prepared; the worker proves it aligned that same file by its hash.
            served_sha = stored["manifest"]["files"][0]["sha256"]
            if result.get("epub_sha256") != served_sha:
                raise ValueError("the worker aligned a different EPUB than the one served")
            alignment_map = result.get("alignment_map")
            if alignment_map is None:
                # The worker's own publish was vetoed against the map it was sent:
                # the book keeps its incumbent, as a local job would.
                if not sm.alignment_service._get_alignment(book.abs_id):
                    raise ValueError("worker kept the existing map, but this book has none")
                outcome = "retained"
            else:
                publish_args = _publish_args(result)
                published = sm.alignment_service._publish_map(book.abs_id, alignment_map, **publish_args)
                outcome = "published" if published else "vetoed"

            # Warm the parse cache, as the end of a local job does.
            sm.ebook_parser.extract_text_and_map(epub_path)
            transcript_source = result.get("transcript_source")
            sm._finish_alignment_success(
                book, epub_path,
                transcript_source if isinstance(transcript_source, str) else None,
                self._persist_for(lease_id, book),
            )
        logger.info(
            "✅ Alignment worker '%s' finished '%s' (%s)",
            sanitize_log_data(worker), sanitize_log_data(book.abs_title), outcome,
        )
        return {"status": outcome}

    def fail(self, lease_id: str, failure: dict) -> None:
        """Record a worker's failure through the local job's retry logic. Raises LeaseGone."""
        with self.database_service._alignment_lease_lock:
            lease = self.database_service.get_alignment_lease(lease_id)
            if lease is None:
                raise LeaseGone(lease_id)
            job, book = lease
            worker = job.lease_owner or "unknown worker"
            error_class = _FAILURE_CLASSES.get(str(failure.get("error_class") or ""), RuntimeError)
            message = str(failure.get("message") or "alignment failed")[:2000]
            logger.error(
                "❌ %s: alignment worker '%s' failed: %s",
                sanitize_log_data(book.abs_title), sanitize_log_data(worker), sanitize_log_data(message),
            )
            self.sync_manager._record_alignment_failure(
                book, error_class(f"{message} (on worker {worker})"), None,
            )


def _publish_args(result: dict) -> dict:
    """``_publish_map`` keyword arguments from a worker's result, validated."""
    alignment_map = result.get("alignment_map")
    if not isinstance(alignment_map, list) or not all(isinstance(p, dict) for p in alignment_map):
        raise ValueError("alignment_map must be a list of points")
    align_method = result.get("align_method")
    if not isinstance(align_method, str) or not align_method or len(align_method) > 50:
        raise ValueError("align_method must be a short string")
    total_chars = result.get("total_chars")
    if total_chars is not None and not isinstance(total_chars, int):
        raise ValueError("total_chars must be an integer")
    exclude_spans = result.get("exclude_spans")
    if exclude_spans is not None:
        exclude_spans = [(int(lo), int(hi)) for lo, hi in exclude_spans]
    segments = result.get("segments")
    if segments is not None:
        segments = [Segment(**segment) for segment in segments]
    return {
        "align_method": align_method,
        "total_chars": total_chars,
        "exclude_spans": exclude_spans,
        "segments": segments,
    }


def result_from_publish(published: Optional[dict], book, transcript_source: Optional[str]) -> dict:
    """A worker's result payload from the last map its pipeline stored."""
    payload: dict[str, Any] = {
        "transcript_source": transcript_source,
        "alignment_map": None,
    }
    if published is not None:
        segments = published.get("segments")
        payload.update({
            "alignment_map": published["alignment_map"],
            "align_method": published["align_method"],
            "total_chars": published.get("total_chars"),
            "exclude_spans": (
                [list(span) for span in published["exclude_spans"]]
                if published.get("exclude_spans") is not None else None
            ),
            "segments": (
                [{
                    "char_start": s.char_start, "char_end": s.char_end,
                    "ts_start": s.ts_start, "ts_end": s.ts_end,
                    "inliers": s.inliers, "residual": s.residual,
                } for s in segments]
                if segments is not None else None
            ),
        })
    return payload
