"""
Write-suppression tracker — prevents self-triggered feedback loops.

Call record_write(client_name, abs_id) after BookBridge successfully pushes
progress to any client. Call is_own_write(client_name, abs_id) before acting
on a progress change from that client to suppress round-trip echoes.

Supported client_name values: 'ABS', 'Storyteller', 'BookLore', 'BookOrbit', 'KoSync'
"""

import threading
import time

_recent_writes: dict[str, tuple[float, float | None]] = {}
_writes_lock = threading.Lock()

_DEFAULT_SUPPRESSION_WINDOW = 60  # seconds


def _cleanup_stale_locked(now: float, suppression_window: int) -> None:
    stale = [k for k, v in _recent_writes.items() if now - v[0] > suppression_window]
    for k in stale:
        del _recent_writes[k]


def _resolve_uid(user_id):
    """Fall back to the ambient sync user (set by sync_cycle) so record and read
    key on the same user even when a caller deep in a client doesn't thread
    user_id through. Keeps one user's push from suppressing another's change."""
    if user_id is not None:
        return user_id
    try:
        from src.utils.user_context import get_current_user_id
        return get_current_user_id()
    except Exception:
        return None


def _key(client_name: str, abs_id: str, user_id=None) -> str:
    return f"{user_id}:{client_name}:{abs_id}"


def record_write(client_name: str, abs_id: str, pct: float | None = None, user_id=None) -> None:
    """Call after BookBridge successfully pushes progress to a client.

    Multi-user: suppression is per (user, client, book) so one user's push
    never suppresses another user's genuine change on the same book."""
    key = _key(client_name, abs_id, _resolve_uid(user_id))
    with _writes_lock:
        _recent_writes[key] = (time.time(), pct)


def get_recent_write(client_name: str, abs_id: str, suppression_window: int = _DEFAULT_SUPPRESSION_WINDOW, user_id=None) -> dict | None:
    """
    Return recent write metadata for client/book if still inside suppression window.

    Metadata includes:
    - ts: write timestamp
    - age: seconds since write
    - pct: written percentage if provided by caller
    """
    key = _key(client_name, abs_id, _resolve_uid(user_id))
    with _writes_lock:
        now = time.time()
        entry = _recent_writes.get(key)
        if not entry:
            _cleanup_stale_locked(now, suppression_window)
            return None

        last_write_ts, pct = entry
        age = now - last_write_ts
        if age < suppression_window:
            return {"ts": last_write_ts, "age": age, "pct": pct}

        _cleanup_stale_locked(now, suppression_window)
        return None


def is_own_write(client_name: str, abs_id: str, suppression_window: int = _DEFAULT_SUPPRESSION_WINDOW, user_id=None) -> bool:
    """Return True if a recent progress event for this client/book was caused by our own write."""
    return get_recent_write(client_name, abs_id, suppression_window, user_id=user_id) is not None
