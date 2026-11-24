"""Non-blocking, source-preserving thumbnails for authenticated library covers."""

import json
import logging
import os
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

logger = logging.getLogger(__name__)


class CoverThumbnailCache:
    """Generate thumbnails off the request thread without duplicate work."""

    def __init__(self, workers: int = 2) -> None:
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="cover-thumb")
        self._lock = threading.Lock()
        self._pending: set[str] = set()
        self._failures: dict[str, tuple[list[int], float]] = {}

    @staticmethod
    def _signature(source: Path) -> list[int]:
        stat = source.stat()
        return [stat.st_size, stat.st_mtime_ns]

    def is_current(self, source: Path, target: Path) -> bool:
        """Require an exact source fingerprint, including same-mtime rewrites."""
        try:
            if not target.is_file() or target.stat().st_size == 0:
                return False
            recorded = json.loads(target.with_suffix(".json").read_text(encoding="utf-8"))
            return recorded == self._signature(source)
        except (OSError, ValueError, TypeError):
            return False

    def generate(self, source: Path, target: Path) -> bool:
        """Write a JPEG preview atomically; never write to the original artwork."""
        try:
            signature = self._signature(source)
            if self.is_current(source, target):
                return True
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="cover-thumb-", dir=target.parent) as scratch:
                output = Path(scratch) / "image.jpg"
                metadata = Path(scratch) / "image.json"
                command = [
                    "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                    "-threads", "1", "-i", str(source), "-frames:v", "1",
                    "-vf", "scale=w='min(720,iw)':h='min(960,ih)':"
                    "force_original_aspect_ratio=decrease:force_divisible_by=2",
                    "-q:v", "4", "-y", str(output),
                ]
                subprocess.run(command, check=True, capture_output=True, timeout=30)
                if not output.is_file() or output.stat().st_size == 0:
                    return False
                if self._signature(source) != signature:
                    return False  # source was rewritten while FFmpeg was reading it
                metadata.write_text(json.dumps(signature), encoding="utf-8")
                os.replace(output, target)
                os.replace(metadata, target.with_suffix(".json"))
            return True
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            logger.warning("Cover thumbnail generation failed for %s: %s", source.name, exc, exc_info=True)
            return False

    def failed_recently(self, source: Path, target: Path) -> bool:
        """Temporarily suppress retries of a failed, unchanged source image."""
        key = str(target.absolute())
        try:
            signature = self._signature(source)
        except OSError:
            return False
        with self._lock:
            cached = self._failures.get(key)
            return bool(cached and cached[0] == signature and cached[1] > time.monotonic())

    def enqueue(self, source: Path, target: Path) -> bool:
        """Schedule at most one outstanding encode per destination."""
        key = str(target.absolute())
        if self.failed_recently(source, target):
            return False
        try:
            queued_signature = self._signature(source)
        except OSError:
            return False
        with self._lock:
            if key in self._pending:
                return False
            self._pending.add(key)
        try:
            future = self._pool.submit(self.generate, source, target)
        except RuntimeError:
            with self._lock:
                self._pending.discard(key)
            return False

        def complete(_future):
            try:
                succeeded = bool(_future.result())
            except Exception:
                logger.warning("Unexpected cover thumbnail worker failure", exc_info=True)
                succeeded = False
            try:
                signature = self._signature(source)
            except OSError:
                signature = []
            with self._lock:
                self._pending.discard(key)
                if succeeded:
                    self._failures.pop(key, None)
                elif signature == queued_signature:
                    self._failures[key] = (signature, time.monotonic() + 60)
                else:
                    self._failures.pop(key, None)  # the original changed mid-encode

        future.add_done_callback(complete)
        return True


cover_thumbnail_cache = CoverThumbnailCache()
