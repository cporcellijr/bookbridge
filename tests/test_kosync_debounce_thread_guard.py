"""The real KoSync debounce loop must never run under the test suite: once started it
ticks every 10 s for the rest of the run and finalizes later tests' open sessions."""

import threading
from unittest.mock import patch

from src.api import kosync_server


def _debounce_threads():
    return [t for t in threading.enumerate() if t.name == "kosync-debounce"]


def test_a_test_that_records_a_kosync_event_starts_no_debounce_loop(monkeypatch):
    monkeypatch.setattr(kosync_server, "_debounce_thread_started", False)
    before = _debounce_threads()
    try:
        kosync_server._record_kosync_event("guard-book", "Guard", user_id=1)
    finally:
        with kosync_server._kosync_debounce_lock:
            kosync_server._kosync_debounce.clear()
    assert _debounce_threads() == before


def test_the_process_starts_the_debounce_loop_once(monkeypatch):
    monkeypatch.undo()  # the real helper, not the suite's no-op
    monkeypatch.setattr(kosync_server, "_debounce_thread_started", False)
    with patch.object(kosync_server.threading, "Thread") as thread_cls:
        kosync_server._start_kosync_debounce_thread()
        kosync_server._start_kosync_debounce_thread()
    thread_cls.assert_called_once_with(
        target=kosync_server._kosync_debounce_loop, daemon=True, name="kosync-debounce",
    )
    thread_cls.return_value.start.assert_called_once()
