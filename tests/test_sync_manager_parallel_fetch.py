from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.sync_manager import SyncManager


def test_completed_state_fetch_is_not_discarded_as_a_timeout():
    manager = SyncManager.__new__(SyncManager)
    state = SimpleNamespace(current={})
    client = MagicMock()
    client.get_service_state.return_value = state
    manager.sync_clients = {"KoSync": client}

    def force_old_timeout(futures, timeout):
        return set(), set(futures)

    with patch("src.sync_manager.wait", side_effect=force_old_timeout, create=True), \
         patch("src.sync_manager._STATE_FETCH_SLOW_SECONDS", -1), \
         patch("src.sync_manager.logger") as mock_logger:
        result = manager._fetch_states_parallel(
            SimpleNamespace(),
            {},
            "Book",
        )

    assert result["KoSync"] is state
    assert state.current["_service_prev_updated_at"] is None
    assert any(
        call.args and "state fetch was slow" in str(call.args[0])
        for call in mock_logger.warning.call_args_list
    )
    assert not any(
        call.args and "timed out" in str(call.args[0])
        for call in mock_logger.warning.call_args_list
    )
