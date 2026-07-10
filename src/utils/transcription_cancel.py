"""Cooperative cancellation for background transcription workers.

LocalWhisper transcription runs in a daemon thread with no cancellation hook.
When a mapping is deleted mid-transcription the worker would otherwise keep
running: it writes progress checkpoints into a cache directory that the delete
path has already removed (FileNotFoundError) and finally tries to persist a
Book row that no longer exists (StaleDataError).

This module is a tiny thread-safe registry of abs_ids whose transcription has
been asked to stop. The delete path calls ``request_cancel`` before tearing
down resources; the worker checks ``is_cancelled`` at each chunk boundary and
exits cleanly, and clears the flag when it finishes.
"""

import threading

_lock = threading.Lock()
_cancelled: set[str] = set()


def request_cancel(abs_id) -> None:
    """Mark a book's transcription for cooperative cancellation."""
    if abs_id is None:
        return
    with _lock:
        _cancelled.add(str(abs_id))


def is_cancelled(abs_id) -> bool:
    """Return True if transcription for this book has been asked to stop."""
    if abs_id is None:
        return False
    with _lock:
        return str(abs_id) in _cancelled


def clear_cancel(abs_id) -> None:
    """Drop any pending cancellation flag for a book (worker teardown)."""
    if abs_id is None:
        return
    with _lock:
        _cancelled.discard(str(abs_id))
