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
    # Per-user poll gates on the user's claimed books; these fixtures model books
    # shared/claimed by every user under test.
    db.get_linked_abs_ids.return_value = {b.abs_id for b in books}
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


def test_poller_skips_book_not_claimed_by_user(monkeypatch):
    """A per-user poll must skip a book the user has not claimed — before making
    the network call — since the catalog is shared."""
    with write_tracker._writes_lock:
        write_tracker._recent_writes.clear()
    monkeypatch.setattr(client_poller_module.threading, "Thread", _ImmediateThread)

    book = SimpleNamespace(abs_id="abs-1", abs_title="Shared Book")
    users = [SimpleNamespace(id=1, active=1)]
    db = _db(users, [book])
    db.get_linked_abs_ids.return_value = set()  # user 1 has NOT claimed abs-1

    client = _client(0.70)
    registry = _Registry({1: _Bundle({"Storyteller": client})})
    sm = MagicMock()
    poller = ClientPoller(db, sm, {}, user_client_registry=registry)
    poller._last_known[(1, "Storyteller", "abs-1")] = 0.20

    poller._poll_client("Storyteller")

    sm.sync_cycle.assert_not_called()
    client.get_service_state.assert_not_called()  # skipped before the network call


def test_per_user_poll_suppresses_global_self_write(monkeypatch):
    """A push recorded by the global (user_id=None) ABS cycle must suppress a
    per-user poll echo — otherwise the admin's instant sync loops forever."""
    with write_tracker._writes_lock:
        write_tracker._recent_writes.clear()
    monkeypatch.setattr(client_poller_module.threading, "Thread", _ImmediateThread)

    book = SimpleNamespace(abs_id="abs-1", abs_title="Shared Book")
    users = [SimpleNamespace(id=1, active=1)]
    db = _db(users, [book])

    registry = _Registry({1: _Bundle({"Storyteller": _client(0.778)})})
    sm = MagicMock()
    poller = ClientPoller(db, sm, {}, user_client_registry=registry)
    poller._last_known[(1, "Storyteller", "abs-1")] = 0.776

    # Global cycle recorded the push under the None namespace, not user 1's.
    write_tracker.record_write("Storyteller", "abs-1", 0.776, user_id=None)

    poller._poll_client("Storyteller")

    sm.sync_cycle.assert_not_called()


def test_settle_fire_rechecks_global_self_write(monkeypatch):
    """A deferred change that settles onto a global self-write must not fire:
    the settle wait can outlast the suppression window."""
    with write_tracker._writes_lock:
        write_tracker._recent_writes.clear()
    monkeypatch.setattr(client_poller_module.threading, "Thread", _ImmediateThread)
    monkeypatch.setenv("STORYTELLER_POLL_WAIT_FOR_SETTLE", "true")

    book = SimpleNamespace(abs_id="abs-1", abs_title="Shared Book")
    users = [SimpleNamespace(id=1, active=1)]
    db = _db(users, [book])

    client = _client(0.60)
    registry = _Registry({1: _Bundle({"Storyteller": client})})
    sm = MagicMock()
    poller = ClientPoller(db, sm, {}, user_client_registry=registry)
    poller._last_known[(1, "Storyteller", "abs-1")] = 0.50

    # Poll 1: genuine-looking change with no write yet → deferred (pending set).
    poller._poll_client("Storyteller")
    sm.sync_cycle.assert_not_called()
    assert (1, "Storyteller", "abs-1") in poller._pending_sync

    # A global (None) push then lands at the settled position before poll 2.
    write_tracker.record_write("Storyteller", "abs-1", 0.60, user_id=None)
    poller._poll_client("Storyteller")

    sm.sync_cycle.assert_not_called()
    assert (1, "Storyteller", "abs-1") not in poller._pending_sync
