"""BookOrbit request-channel watcher: connection, change handling and supervision.

``socketio.Client`` is always patched; no test opens a real connection.
"""
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.services.bookorbit_request_watcher import CHANGED_EVENT, NAMESPACE, BookOrbitRequestWatcher
from src.utils.logging_utils import get_persistent_condition_logger


class FakeSocket:
    """Records handlers and connect args; ``on_wait`` runs while the session is "open"."""

    def __init__(self, on_wait=None):
        self.handlers = {}
        self.connect_kwargs = None
        self.url = None
        self.on_wait = on_wait
        self.disconnected = False

    def on(self, event, handler, namespace=None):
        self.handlers[(event, namespace)] = handler

    def connect(self, url, **kwargs):
        self.url, self.connect_kwargs = url, kwargs
        self.handlers[("connect", NAMESPACE)]()

    def wait(self):
        if self.on_wait:
            self.on_wait(self)

    def disconnect(self):
        self.disconnected = True


def bookorbit_client(configured=True, token="reader-token"):
    client = MagicMock()
    client.is_configured.return_value = configured
    client._get_fresh_token.return_value = token
    client._get_base_url.return_value = "https://orbit.example"
    return client


class TestBookOrbitRequestWatcher(unittest.TestCase):
    def setUp(self):
        get_persistent_condition_logger().reset()
        self.addCleanup(get_persistent_condition_logger().reset)
        self.db = MagicMock()
        self.db.get_user.return_value = SimpleNamespace(id=2, active=1)
        self.client = bookorbit_client()
        self.registry = MagicMock()
        self.registry.get_clients.return_value = SimpleNamespace(bookorbit_client=self.client)
        self.changes = []
        self.watcher = BookOrbitRequestWatcher(
            self.db, self.registry, self.changes.append,
            debounce_secs=0, idle_secs=0, restart_base_secs=0, restart_max_secs=0,
        )

    def test_connects_to_the_request_channel_with_the_readers_own_login(self):
        sockets = []

        def make_socket(**_kwargs):
            sock = FakeSocket(on_wait=lambda _s: self.watcher._stop.set())
            sockets.append(sock)
            return sock

        with patch("src.services.bookorbit_request_watcher.socketio.Client", side_effect=make_socket):
            self.watcher._supervise(2)
        self.assertEqual(len(sockets), 1)
        self.assertEqual(sockets[0].url, "https://orbit.example")
        self.assertEqual(sockets[0].connect_kwargs["namespaces"], [NAMESPACE])
        self.assertEqual(sockets[0].connect_kwargs["auth"], {"token": "reader-token"})
        self.registry.get_clients.assert_called_with(2)
        self.assertTrue(sockets[0].disconnected)

    def test_connect_and_a_burst_of_changes_become_one_check(self):
        def session(sock):
            changed = sock.handlers[(CHANGED_EVENT, NAMESPACE)]
            changed()
            changed()
            self.watcher._stop.set()

        with patch("src.services.bookorbit_request_watcher.socketio.Client",
                   side_effect=lambda **_k: FakeSocket(on_wait=session)):
            self.watcher._supervise(2)
        self.watcher._fire_due()
        self.assertEqual(self.changes, [2])
        self.watcher._fire_due()
        self.assertEqual(self.changes, [2])

    def test_a_change_after_the_check_queues_another(self):
        self.watcher._schedule(2)
        self.watcher._fire_due()
        self.watcher._schedule(2)
        self.watcher._fire_due()
        self.assertEqual(self.changes, [2, 2])

    def test_a_dropped_connection_reconnects_and_checks_again(self):
        sessions = []

        def make_socket(**_kwargs):
            sessions.append(1)
            if len(sessions) == 2:
                return FakeSocket(on_wait=lambda _s: self.watcher._stop.set())
            return FakeSocket()

        with patch("src.services.bookorbit_request_watcher.socketio.Client", side_effect=make_socket):
            self.watcher._supervise(2)
        self.assertEqual(len(sessions), 2)
        self.assertEqual(self.client._get_fresh_token.call_count, 2)

    def test_refused_connection_is_logged_once_and_retried(self):
        attempts = []

        class Refused(FakeSocket):
            def connect(self, url, **kwargs):
                attempts.append(url)
                if len(attempts) == 2:
                    self_watcher._stop.set()
                raise ConnectionError("handshake refused")

        self_watcher = self.watcher
        with patch("src.services.bookorbit_request_watcher.socketio.Client",
                   side_effect=lambda **_k: Refused()), \
                self.assertLogs("src.services.bookorbit_request_watcher", level="WARNING") as logs:
            self.watcher._supervise(2)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(sum("could not watch user 2" in line for line in logs.output), 1)
        self.assertEqual(self.changes, [])

    def test_reader_without_bookorbit_gets_no_connection(self):
        self.registry.get_clients.return_value = SimpleNamespace(bookorbit_client=bookorbit_client(configured=False))
        original_wait = self.watcher._stop.wait

        def stop_on_idle(timeout=None):
            self.watcher._stop.set()
            return original_wait(0)

        with patch("src.services.bookorbit_request_watcher.socketio.Client") as client_cls, \
                patch.object(self.watcher._stop, "wait", side_effect=stop_on_idle):
            self.watcher._supervise(2)
        client_cls.assert_not_called()

    def test_disabled_or_deleted_account_ends_its_supervisor(self):
        for user in (SimpleNamespace(id=2, active=0), None):
            self.db.get_user.return_value = user
            with patch("src.services.bookorbit_request_watcher.socketio.Client") as client_cls:
                self.watcher._supervise(2)
            client_cls.assert_not_called()

    def test_reader_disabled_while_connected_gets_no_check_and_is_disconnected(self):
        sock = FakeSocket()

        def session(_s):
            self.db.get_user.return_value = SimpleNamespace(id=2, active=0)  # admin disables the account
            sock.handlers[(CHANGED_EVENT, NAMESPACE)]()
            self.watcher._fire_due()

        sock.on_wait = session
        with patch("src.services.bookorbit_request_watcher.socketio.Client", return_value=sock):
            self.watcher._supervise(2)
        self.assertEqual(self.changes, [])
        self.assertTrue(sock.disconnected)

    def test_reader_whose_bookorbit_was_switched_off_gets_no_check(self):
        self.watcher._schedule(2)
        self.registry.get_clients.return_value = SimpleNamespace(bookorbit_client=bookorbit_client(configured=False))
        self.watcher._fire_due()
        self.assertEqual(self.changes, [])

    def test_user_scan_supervises_only_active_readers(self):
        self.db.list_users.return_value = [SimpleNamespace(id=1, active=1), SimpleNamespace(id=2, active=0)]
        spawned = []
        with patch.object(self.watcher, "_spawn", side_effect=lambda uid: (spawned.append(uid), self.watcher._stop.set())):
            self.watcher._user_loop()
        self.assertEqual(spawned, [1])

    def test_a_failing_check_does_not_stop_later_checks(self):
        calls = []

        def flaky(user_id):
            calls.append(user_id)
            if user_id == 1:
                raise RuntimeError("BookOrbit down")

        self.watcher._on_change = flaky
        self.watcher._schedule(1)
        self.watcher._schedule(2)
        with self.assertLogs("src.services.bookorbit_request_watcher", level="WARNING"):
            self.watcher._fire_due()
        self.assertEqual(sorted(calls), [1, 2])

    def test_stop_closes_open_connections(self):
        sock = FakeSocket()
        self.watcher._sockets[2] = sock
        self.watcher.stop()
        self.assertTrue(sock.disconnected)
        self.assertTrue(self.watcher._stop.is_set())


class TestWatcherDrivesTheForcedRequestCheck(unittest.TestCase):
    def test_a_change_runs_the_readers_forced_request_check(self):
        from src.sync_manager import SyncManager

        manager = SyncManager.__new__(SyncManager)
        acquisition = MagicMock()
        acquisition.has_work.return_value = True
        bundle = SimpleNamespace(bookorbit_client=bookorbit_client(), shelfmark_client=None, credentials={})
        manager.acquisition_service = acquisition
        manager.user_client_registry = MagicMock(get_clients=MagicMock(return_value=bundle))
        watcher = BookOrbitRequestWatcher(
            MagicMock(), MagicMock(),
            lambda user_id: manager._refresh_acquisition_requests(user_id, force=True),
            debounce_secs=0,
        )
        watcher._schedule(2)
        watcher._fire_due()
        acquisition.refresh_user.assert_called_once_with(
            2, bundle.bookorbit_client, None, library_clients=bundle, force=True,
        )


if __name__ == "__main__":
    unittest.main()
