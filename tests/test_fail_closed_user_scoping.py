"""A failed user or ownership lookup skips the work instead of widening it to the
global/admin scope; the ABS socket URL drops only a trailing /api path."""
import os
import shutil
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.services.client_poller import ClientPoller  # noqa: E402


class _Registry:
    def __init__(self, client):
        self._client = client

    def get_clients(self, user_id):
        return SimpleNamespace(sync_clients={"BookOrbit": self._client})


def _client(pct=0.5):
    c = MagicMock()
    c.is_configured.return_value = True
    c.get_service_state.return_value = SimpleNamespace(current={"pct": pct})
    return c


def _book(abs_id):
    return SimpleNamespace(abs_id=abs_id, abs_title=abs_id, ebook_filename=f"{abs_id}.epub")


# --- #1: per-user poll must fail closed when ownership lookup fails -------------

def test_poll_skips_user_when_ownership_lookup_fails():
    db = MagicMock()
    db.get_linked_abs_ids.side_effect = RuntimeError("database is locked")
    client = _client()
    poller = ClientPoller(db, MagicMock(), {}, user_client_registry=_Registry(client))

    checked = poller._poll_client_for_user(
        "BookOrbit", client, 7, [_book("mine"), _book("not-mine")], False
    )

    assert checked == 0
    client.get_service_state.assert_not_called()


# --- #3: user-listing failure must not fall back to the global/admin scope -----

def _poller_with_failing_user_list():
    db = MagicMock()
    db.list_users.side_effect = RuntimeError("database is locked")
    global_client = _client()
    poller = ClientPoller(db, MagicMock(), {"BookOrbit": global_client},
                          user_client_registry=_Registry(_client()))
    return poller, db


def test_poll_targets_empty_when_user_listing_fails():
    poller, _ = _poller_with_failing_user_list()
    assert poller._poll_targets("BookOrbit") == []


def test_poll_targets_still_global_when_no_users_exist():
    """Control: a legitimately empty user table keeps the single-user fallback."""
    db = MagicMock()
    db.list_users.return_value = []
    global_client = _client()
    poller = ClientPoller(db, MagicMock(), {"BookOrbit": global_client},
                          user_client_registry=_Registry(_client()))
    assert poller._poll_targets("BookOrbit") == [(None, global_client)]


def test_poller_shelf_watch_skipped_when_user_listing_fails():
    poller, _ = _poller_with_failing_user_list()
    watch = MagicMock()
    poller._shelf_watch_services = {"BookOrbit": watch}
    with patch.dict(os.environ, {"BOOKORBIT_POLL_MODE": "custom"}), \
            patch.object(poller, "_poll_client"):
        poller._poll_cycle()
    watch.process_watch_shelf.assert_not_called()


def _manager_with_failing_user_list():
    from src.sync_manager import SyncManager

    mgr = SyncManager.__new__(SyncManager)
    mgr.user_client_registry = MagicMock()
    mgr.database_service = MagicMock()
    mgr.database_service.list_users.side_effect = RuntimeError("database is locked")
    mgr.sync_cycle = MagicMock()
    return mgr


def test_scheduled_sync_skipped_when_user_listing_fails():
    mgr = _manager_with_failing_user_list()
    mgr.run_sync_for_all_users()
    mgr.sync_cycle.assert_not_called()


def test_instant_sync_skipped_when_user_listing_fails():
    mgr = _manager_with_failing_user_list()
    mgr.run_sync_for_all_users(target_abs_id="book-1")
    mgr.sync_cycle.assert_not_called()


def test_scheduled_sync_still_unscoped_when_no_users_exist():
    """Control: no users at all keeps the single default cycle."""
    from src.sync_manager import SyncManager

    mgr = SyncManager.__new__(SyncManager)
    mgr.user_client_registry = MagicMock()
    mgr.database_service = MagicMock()
    mgr.database_service.list_users.return_value = []
    mgr.sync_cycle = MagicMock()
    mgr.run_sync_for_all_users()
    mgr.sync_cycle.assert_called_once_with(target_abs_id=None)


def test_cycle_shelf_watch_skipped_when_user_listing_fails():
    """Copy of TestSyncManagerPerUserShelfWatch.test_global_cycle_iterates_per_user_for_shelf_watch
    (tests/test_bookorbit_multiuser_ownership.py) with list_users failing."""
    from src.db.database_service import DatabaseService
    from src.sync_manager import SyncManager

    temp_dir = tempfile.mkdtemp()
    db = DatabaseService(os.path.join(temp_dir, "sw.db"))
    try:
        watch = MagicMock()
        watch.runs_in_global_cycle.return_value = True
        mgr = SyncManager.__new__(SyncManager)
        mgr.database_service = db
        mgr.user_client_registry = MagicMock()
        mgr.sync_clients = {}
        mgr.shelf_watch_services = [watch]
        mgr._sync_cycle_ebook_cache = {}
        mgr._sync_cycle_local_epub_cache = {}
        mgr._storyteller_epub_ensure_attempted = set()
        mgr._last_library_sync = 0
        mgr._suggestion_lock = threading.Lock()
        mgr._suggestion_in_flight = set()
        db.get_books_by_status = MagicMock(return_value=[])
        db.list_users = MagicMock(side_effect=RuntimeError("database is locked"))

        with patch.dict(os.environ, {"BOOKLORE_POLL_MODE": "global",
                                     "BOOKLORE_SHELF_WATCH_ENABLED": "false"}):
            mgr._sync_cycle_internal()

        watch.process_watch_shelf.assert_not_called()
    finally:
        db.db_manager.close()
        shutil.rmtree(temp_dir, ignore_errors=True)


# --- #4: socket URL must only drop a trailing /api path -------------------------

def _listener_url(server):
    from src.services.abs_socket_listener import ABSSocketListener

    with patch("src.services.abs_socket_listener.socketio.Client"):
        return ABSSocketListener(server, "tok", MagicMock(), MagicMock())._server_url


def test_socket_url_keeps_api_hostname():
    assert _listener_url("https://api.example.com") == "https://api.example.com"
    assert _listener_url("https://apis.home.lan/") == "https://apis.home.lan"


def test_socket_url_strips_trailing_api_path():
    assert _listener_url("http://abs.local:13378/api") == "http://abs.local:13378"
    assert _listener_url("http://abs.local:13378/api/") == "http://abs.local:13378"
    assert _listener_url("https://host/audiobookshelf/api") == "https://host/audiobookshelf"
    assert _listener_url("http://abs.local:13378") == "http://abs.local:13378"
