"""Detect a mounted original audio file changing under a running job."""

import logging
import os
from typing import Dict, Iterable, Optional, Tuple

logger = logging.getLogger(__name__)

Fingerprint = Tuple[int, int]


class SourceAudioChanged(RuntimeError):
    """A mounted source track changed or vanished after the job acquired it."""


class AcquiredPaths(list):
    """Audio paths plus the fingerprints of any mounted originals among them."""

    fingerprints: Dict[str, Fingerprint]

    def __init__(self, paths: Iterable[str], fingerprints: Optional[Dict[str, Fingerprint]] = None):
        super().__init__(paths)
        self.fingerprints = dict(fingerprints or {})


def fingerprint(path: str) -> Fingerprint:
    """(size, mtime_ns) of ``path``; cheap, no content hashing."""
    stat = os.stat(path)
    return stat.st_size, stat.st_mtime_ns


def verify_unchanged(abs_id: str, fingerprints: Optional[Dict[str, Fingerprint]], seam: str) -> None:
    """Raise SourceAudioChanged unless every fingerprinted file still matches.

    The job then fails through the normal failure/retry path, and the retry
    re-acquires the audio (falling back to download if local validation fails).
    """
    for path, expected in (fingerprints or {}).items():
        try:
            current = fingerprint(path)
        except OSError as exc:
            logger.warning(
                "Source audio check: '%s' is no longer readable before %s for %s (%s); "
                "failing the job so a retry re-acquires the audio",
                path, seam, abs_id, type(exc).__name__, exc_info=True,
            )
            raise SourceAudioChanged(f"source audio vanished: {path}") from exc
        if current != expected:
            logger.warning(
                "Source audio check: '%s' changed before %s for %s (size/mtime %s -> %s); "
                "failing the job so a retry re-acquires the audio",
                path, seam, abs_id, expected, current,
            )
            raise SourceAudioChanged(f"source audio changed: {path}")
