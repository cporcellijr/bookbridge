from types import SimpleNamespace
from unittest.mock import MagicMock

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import src.services.client_poller as client_poller_module
from src.services.client_poller import ClientPoller
from src.services import write_tracker


class _ImmediateThread:
    def __init__(self, target=None, kwargs=None, daemon=None):
        self._target = target
        self._kwargs = kwargs or {}

    def start(self):
        if self._target:
            self._target(**self._kwargs)


def _clear_write_tracker():
    with write_tracker._writes_lock:
        write_tracker._recent_writes.clear()


def test_storyteller_poller_ignores_nearby_self_echo(monkeypatch):
    _clear_write_tracker()
    monkeypatch.setattr(client_poller_module.threading, "Thread", _ImmediateThread)

    book = SimpleNamespace(abs_id="abs-1", abs_title="Book 1")
    db = MagicMock()
    db.get_books_by_status.return_value = [book]

    sync_manager = MagicMock()
    sync_client = MagicMock()
    sync_client.is_configured.return_value = True
    sync_client.get_service_state.return_value = SimpleNamespace(current={"pct": 0.778})

    poller = ClientPoller(db, sync_manager, {"Storyteller": sync_client})
    poller._last_known[("Storyteller", "abs-1")] = 0.776

    write_tracker.record_write("Storyteller", "abs-1", 0.776)
    poller._poll_client("Storyteller")

    sync_manager.sync_cycle.assert_not_called()


def test_storyteller_poller_allows_large_jump_during_suppression(monkeypatch):
    _clear_write_tracker()
    monkeypatch.setattr(client_poller_module.threading, "Thread", _ImmediateThread)

    book = SimpleNamespace(abs_id="abs-1", abs_title="Book 1")
    db = MagicMock()
    db.get_books_by_status.return_value = [book]

    sync_manager = MagicMock()
    sync_client = MagicMock()
    sync_client.is_configured.return_value = True
    sync_client.get_service_state.return_value = SimpleNamespace(current={"pct": 0.85})

    poller = ClientPoller(db, sync_manager, {"Storyteller": sync_client})
    poller._last_known[("Storyteller", "abs-1")] = 0.776

    write_tracker.record_write("Storyteller", "abs-1", 0.776)
    poller._poll_client("Storyteller")

    sync_manager.sync_cycle.assert_called_once_with(target_abs_id="abs-1")


def _make_settle_poller(monkeypatch, initial_pct):
    _clear_write_tracker()
    monkeypatch.setattr(client_poller_module.threading, "Thread", _ImmediateThread)
    monkeypatch.setenv("STORYTELLER_POLL_WAIT_FOR_SETTLE", "true")

    book = SimpleNamespace(abs_id="abs-1", abs_title="Book 1")
    db = MagicMock()
    db.get_books_by_status.return_value = [book]

    sync_manager = MagicMock()
    sync_client = MagicMock()
    sync_client.is_configured.return_value = True

    poller = ClientPoller(db, sync_manager, {"Storyteller": sync_client})
    poller._last_known[("Storyteller", "abs-1")] = initial_pct
    return poller, sync_manager, sync_client


def test_settle_wait_defers_sync_while_position_moves(monkeypatch):
    poller, sync_manager, sync_client = _make_settle_poller(monkeypatch, 0.50)

    # Position keeps moving across two polls: sync must be held back.
    sync_client.get_service_state.return_value = SimpleNamespace(current={"pct": 0.52})
    poller._poll_client("Storyteller")
    sync_client.get_service_state.return_value = SimpleNamespace(current={"pct": 0.54})
    poller._poll_client("Storyteller")

    sync_manager.sync_cycle.assert_not_called()
    assert ("Storyteller", "abs-1") in poller._pending_sync


def test_settle_wait_triggers_sync_once_position_settles(monkeypatch):
    poller, sync_manager, sync_client = _make_settle_poller(monkeypatch, 0.50)

    sync_client.get_service_state.return_value = SimpleNamespace(current={"pct": 0.52})
    poller._poll_client("Storyteller")
    sync_manager.sync_cycle.assert_not_called()

    # Same position on the next poll → settled → one sync cycle.
    poller._poll_client("Storyteller")
    sync_manager.sync_cycle.assert_called_once_with(target_abs_id="abs-1")
    assert ("Storyteller", "abs-1") not in poller._pending_sync

    # Further unchanged polls must not re-trigger.
    poller._poll_client("Storyteller")
    sync_manager.sync_cycle.assert_called_once_with(target_abs_id="abs-1")


def test_settle_wait_disabled_keeps_immediate_trigger(monkeypatch):
    poller, sync_manager, sync_client = _make_settle_poller(monkeypatch, 0.50)
    monkeypatch.setenv("STORYTELLER_POLL_WAIT_FOR_SETTLE", "false")

    sync_client.get_service_state.return_value = SimpleNamespace(current={"pct": 0.52})
    poller._poll_client("Storyteller")

    sync_manager.sync_cycle.assert_called_once_with(target_abs_id="abs-1")
    assert ("Storyteller", "abs-1") not in poller._pending_sync


def test_settle_wait_defers_external_jump_during_suppression(monkeypatch):
    poller, sync_manager, sync_client = _make_settle_poller(monkeypatch, 0.776)

    write_tracker.record_write("Storyteller", "abs-1", 0.776)
    sync_client.get_service_state.return_value = SimpleNamespace(current={"pct": 0.85})
    poller._poll_client("Storyteller")

    sync_manager.sync_cycle.assert_not_called()
    assert ("Storyteller", "abs-1") in poller._pending_sync

    # Position holds on the next poll → the jump syncs now.
    poller._poll_client("Storyteller")
    sync_manager.sync_cycle.assert_called_once_with(target_abs_id="abs-1")


def test_settle_wait_ignores_self_echo_without_pending(monkeypatch):
    poller, sync_manager, sync_client = _make_settle_poller(monkeypatch, 0.776)

    write_tracker.record_write("Storyteller", "abs-1", 0.776)
    sync_client.get_service_state.return_value = SimpleNamespace(current={"pct": 0.778})
    poller._poll_client("Storyteller")
    poller._poll_client("Storyteller")

    sync_manager.sync_cycle.assert_not_called()
    assert ("Storyteller", "abs-1") not in poller._pending_sync
