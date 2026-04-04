"""
Write-suppression tracker — prevents self-triggered feedback loops.

Call record_write(client_name, abs_id) after BookBridge successfully pushes
progress to any client. Call is_own_write(client_name, abs_id) before acting
on a progress change from that client to suppress round-trip echoes.

Supported client_name values: 'ABS', 'Storyteller', 'Grimmory', 'KoSync'
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


def record_write(client_name: str, abs_id: str, pct: float | None = None) -> None:
    """Call after BookBridge successfully pushes progress to a client."""
    key = f"{client_name}:{abs_id}"
    with _writes_lock:
        _recent_writes[key] = (time.time(), pct)


def get_recent_write(client_name: str, abs_id: str, suppression_window: int = _DEFAULT_SUPPRESSION_WINDOW) -> dict | None:
    """
    Return recent write metadata for client/book if still inside suppression window.

    Metadata includes:
    - ts: write timestamp
    - age: seconds since write
    - pct: written percentage if provided by caller
    """
    key = f"{client_name}:{abs_id}"
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


def is_own_write(client_name: str, abs_id: str, suppression_window: int = _DEFAULT_SUPPRESSION_WINDOW) -> bool:
    """Return True if a recent progress event for this client/book was caused by our own write."""
    return get_recent_write(client_name, abs_id, suppression_window) is not None
