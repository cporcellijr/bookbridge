import os
from unittest.mock import Mock, patch

import pytest

from src.api import kosync_server


class _StopLoop(Exception):
    pass


def test_kosync_debounce_worker_uses_updated_setting_without_restart():
    manager = Mock()
    manager.run_sync_for_all_users = Mock()
    original_manager = kosync_server._manager
    kosync_server._manager = manager
    kosync_server._kosync_debounce.clear()
    kosync_server._kosync_debounce[("book-1", None)] = {
        "abs_id": "book-1",
        "title": "Test Book",
        "user_id": None,
        "last_event": 60.0,
        "synced": False,
    }

    sleep_calls = 0

    def fake_sleep(_seconds):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls == 1:
            os.environ["KOSYNC_PUT_DEBOUNCE_SECONDS"] = "30"
            return
        raise _StopLoop

    try:
        with patch.dict(os.environ, {"KOSYNC_PUT_DEBOUNCE_SECONDS": "300"}, clear=False), \
             patch.object(kosync_server.time, "sleep", side_effect=fake_sleep), \
             patch.object(kosync_server.time, "time", return_value=100.0), \
             patch.object(kosync_server, "_flush_stale_kosync_sessions"), \
             patch.object(kosync_server.threading, "Thread") as thread_cls:
            with pytest.raises(_StopLoop):
                kosync_server._kosync_debounce_loop()

        thread_cls.assert_called_once()
        _, kwargs = thread_cls.call_args
        assert kwargs["target"] is manager.run_sync_for_all_users
        assert kwargs["kwargs"] == {"target_abs_id": "book-1"}
    finally:
        kosync_server._kosync_debounce.clear()
        kosync_server._manager = original_manager
