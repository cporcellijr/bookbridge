from types import SimpleNamespace
from unittest.mock import MagicMock

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


class _Bundle:
    def __init__(self, sync_clients):
        self.sync_clients = sync_clients


class _Registry:
    def __init__(self, mapping):
        self._mapping = mapping

    def get_clients(self, user_id):
        return self._mapping[user_id]


def _client(pct):
    c = MagicMock()
    c.is_configured.return_value = True
    c.get_service_state.return_value = SimpleNamespace(current={"pct": pct})
    return c


def _db(users, books):
    db = MagicMock()
    db.get_books_by_status.return_value = books
    db.list_users.return_value = users
    return db


def test_poller_triggers_per_user_sync(monkeypatch):
    with write_tracker._writes_lock:
        write_tracker._recent_writes.clear()
    monkeypatch.setattr(client_poller_module.threading, "Thread", _ImmediateThread)

    book = SimpleNamespace(abs_id="abs-1", abs_title="Shared Book")
    users = [SimpleNamespace(id=1, active=1), SimpleNamespace(id=2, active=1)]
    db = _db(users, [book])

    # Each user's Storyteller account is at a different position.
    registry = _Registry({
        1: _Bundle({"Storyteller": _client(0.30)}),
        2: _Bundle({"Storyteller": _client(0.70)}),
    })
    sm = MagicMock()
    poller = ClientPoller(db, sm, {}, user_client_registry=registry)
    # Prior cached positions so a change is detected for both users.
    poller._last_known[(1, "Storyteller", "abs-1")] = 0.20
    poller._last_known[(2, "Storyteller", "abs-1")] = 0.60

    poller._poll_client("Storyteller")

    calls = {(c.kwargs["target_abs_id"], c.kwargs["user_id"]) for c in sm.sync_cycle.call_args_list}
    assert calls == {("abs-1", 1), ("abs-1", 2)}


def test_poller_skips_users_without_configured_client(monkeypatch):
    with write_tracker._writes_lock:
        write_tracker._recent_writes.clear()
    monkeypatch.setattr(client_poller_module.threading, "Thread", _ImmediateThread)

    book = SimpleNamespace(abs_id="abs-1", abs_title="Shared Book")
    users = [SimpleNamespace(id=1, active=1), SimpleNamespace(id=2, active=1)]
    db = _db(users, [book])

    unconfigured = MagicMock()
    unconfigured.is_configured.return_value = False
    registry = _Registry({
        1: _Bundle({"Storyteller": _client(0.30)}),
        2: _Bundle({"Storyteller": unconfigured}),
    })
    sm = MagicMock()
    poller = ClientPoller(db, sm, {}, user_client_registry=registry)
    poller._last_known[(1, "Storyteller", "abs-1")] = 0.20

    poller._poll_client("Storyteller")

    calls = [(c.kwargs["target_abs_id"], c.kwargs["user_id"]) for c in sm.sync_cycle.call_args_list]
    assert calls == [("abs-1", 1)]
