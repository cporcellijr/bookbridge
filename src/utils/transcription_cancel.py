"""Generation-safe cooperative cancellation for transcription workers."""

import threading
from dataclasses import dataclass, field
from typing import Optional

_lock = threading.Lock()
_active: dict[str, "CancellationToken"] = {}


@dataclass(eq=False)
class CancellationToken:
    """Cancellation state owned by one specific background worker generation."""

    abs_id: str
    event: threading.Event = field(default_factory=threading.Event)

    def cancel(self) -> None:
        """Ask this worker generation to stop."""
        self.event.set()

    def is_cancelled(self) -> bool:
        """Return whether this worker generation has been asked to stop."""
        return self.event.is_set()


def register_worker(abs_id: str) -> CancellationToken:
    """Register and return the cancellation token for a new worker generation."""
    token = CancellationToken(str(abs_id))
    with _lock:
        _active[token.abs_id] = token
    return token


def request_cancel(abs_id: str) -> bool:
    """Cancel the active worker for ``abs_id`` without creating sticky state."""
    if abs_id is None:
        return False
    with _lock:
        token = _active.get(str(abs_id))
    if token is None:
        return False
    token.cancel()
    return True


def is_cancelled(abs_id: str, token: Optional[CancellationToken] = None) -> bool:
    """Return whether the specified/current worker generation is cancelled."""
    if token is not None:
        return token.is_cancelled()
    if abs_id is None:
        return False
    with _lock:
        current = _active.get(str(abs_id))
    return bool(current and current.is_cancelled())


def unregister_worker(abs_id: str, token: CancellationToken) -> None:
    """Remove ``token`` only if it is still the active worker generation."""
    if abs_id is None or token is None:
        return
    with _lock:
        key = str(abs_id)
        if _active.get(key) is token:
            _active.pop(key, None)


def active_workers() -> dict[str, CancellationToken]:
    """Snapshot of the currently registered worker generations by book id."""
    with _lock:
        return dict(_active)


def run_unless_other_worker(book_id, own_token, action, key=str):
    """Run ``action()`` while holding the registry lock, only if no other generation is live.

    ``key`` maps ids to a comparison key (so ``a:1`` and ``a_1`` can be treated as one book).
    A worker registering concurrently either registers first (the action is skipped) or after
    the action finished. ``action`` must be fast and must not register or unregister workers.
    Returns ``(ran, result)``.
    """
    wanted = key(book_id)
    with _lock:
        if any(key(other) == wanted and token is not own_token for other, token in _active.items()):
            return False, None
        return True, action()
