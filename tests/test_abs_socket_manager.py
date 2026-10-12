"""Tests for ABSSocketManager — per-user ABS Socket.IO listener orchestration."""

import os
import unittest
from unittest.mock import MagicMock, patch

from src.services.abs_socket_manager import ABSSocketManager
from src.utils.user_config import _ALLOW_GLOBAL_FALLBACK_KEY


def _user(user_id, active=1):
    u = MagicMock()
    u.id = user_id
    u.active = active
    return u


def _bundle(token, configured=True, allow_global_fallback=False):
    """Build a fake per-user client bundle exposing an ABS client + credentials."""
    abs_client = MagicMock()
    abs_client.is_configured.return_value = configured
    abs_sync = MagicMock()
    abs_sync.abs_client = abs_client
    bundle = MagicMock()
    bundle.sync_clients = {"ABS": abs_sync}
    creds = {_ALLOW_GLOBAL_FALLBACK_KEY: allow_global_fallback}
    if token is not None:
        creds["ABS_KEY"] = token
    bundle.credentials = creds
    return bundle


class TestABSSocketManagerTargets(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"ABS_SERVER": "http://abs.local", "ABS_KEY": "admin-token"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.db = MagicMock()
        self.sync = MagicMock()
        # A bare MagicMock returns a truthy child for is_primary_admin(), which
        # would silently make whichever user a test happens to list the primary
        # admin and so decide the global listener's fate. Default it to False so
        # every test that cares about the primary admin says so explicitly.
        self.db.is_primary_admin.return_value = False

    def test_global_only_when_no_registry(self):
        """Without a registry, only the global listener target is returned (fail-open)."""
        mgr = ABSSocketManager(self.db, self.sync, user_client_registry=None)
        targets = mgr._listener_targets()
        self.assertEqual(targets, [(None, "http://abs.local", "admin-token")])

    def test_no_targets_when_no_token_and_no_registry(self):
        """No global token and no registry yields no listeners."""
        with patch.dict(os.environ, {"ABS_KEY": ""}):
            mgr = ABSSocketManager(self.db, self.sync, user_client_registry=None)
            self.assertEqual(mgr._listener_targets(), [])

    def test_adds_per_user_listener_for_distinct_token(self):
        """A regular user with their own ABS token gets a scoped listener."""
        self.db.list_users.return_value = [_user(2)]
        registry = MagicMock()
        registry.get_clients.return_value = _bundle("caitlin-token")
        mgr = ABSSocketManager(self.db, self.sync, user_client_registry=registry)

        targets = mgr._listener_targets()

        self.assertIn((None, "http://abs.local", "admin-token"), targets)
        self.assertIn((2, "http://abs.local", "caitlin-token"), targets)
        self.assertEqual(len(targets), 2)

    def test_admin_token_not_double_listened(self):
        """An admin whose token falls back to the global key is not duplicated."""
        self.db.list_users.return_value = [_user(1)]
        registry = MagicMock()
        # Admin: no own ABS_KEY, allowed global fallback -> resolves to admin-token.
        registry.get_clients.return_value = _bundle(
            token=None, allow_global_fallback=True
        )
        mgr = ABSSocketManager(self.db, self.sync, user_client_registry=registry)

        targets = mgr._listener_targets()

        self.assertEqual(targets, [(None, "http://abs.local", "admin-token")])

    def test_skips_user_with_unconfigured_abs(self):
        """A user without a configured ABS client gets no listener."""
        self.db.list_users.return_value = [_user(3)]
        registry = MagicMock()
        registry.get_clients.return_value = _bundle("x", configured=False)
        mgr = ABSSocketManager(self.db, self.sync, user_client_registry=registry)

        self.assertEqual(mgr._listener_targets(), [(None, "http://abs.local", "admin-token")])

    def test_inactive_users_skipped(self):
        """Inactive users are not given listeners."""
        self.db.list_users.return_value = [_user(4, active=0)]
        registry = MagicMock()
        registry.get_clients.return_value = _bundle("other-token")
        mgr = ABSSocketManager(self.db, self.sync, user_client_registry=registry)

        self.assertEqual(mgr._listener_targets(), [(None, "http://abs.local", "admin-token")])
        registry.get_clients.assert_not_called()

    def test_two_users_sharing_token_deduped(self):
        """Two users with the same token only produce one extra listener."""
        self.db.list_users.return_value = [_user(5), _user(6)]
        registry = MagicMock()
        registry.get_clients.side_effect = [
            _bundle("shared-token"),
            _bundle("shared-token"),
        ]
        mgr = ABSSocketManager(self.db, self.sync, user_client_registry=registry)

        targets = mgr._listener_targets()

        tokens = [t for _, _, t in targets]
        self.assertEqual(tokens.count("shared-token"), 1)

    # --- New tests for global ABS_ENABLED gating ---

    def test_global_target_skipped_when_primary_admin_abs_disabled(self):
        """Global target NOT returned when primary admin's ABS client is unconfigured."""
        # Primary admin (user 1) has ABS disabled
        self.db.list_users.return_value = [_user(1)]
        self.db.is_primary_admin.return_value = True
        registry = MagicMock()
        registry.get_clients.return_value = _bundle("admin-token", configured=False)
        mgr = ABSSocketManager(self.db, self.sync, user_client_registry=registry)

        targets = mgr._listener_targets()

        # Global target should be skipped
        self.assertEqual(targets, [])

    def test_global_target_returned_when_primary_admin_abs_enabled(self):
        """Global target IS returned when primary admin has ABS enabled."""
        self.db.list_users.return_value = [_user(1)]
        self.db.is_primary_admin.return_value = True
        registry = MagicMock()
        registry.get_clients.return_value = _bundle("admin-token", configured=True)
        mgr = ABSSocketManager(self.db, self.sync, user_client_registry=registry)

        targets = mgr._listener_targets()

        self.assertEqual(targets, [(None, "http://abs.local", "admin-token")])

    def test_global_target_fail_open_when_no_registry(self):
        """Fail-open: global target returned when there is no registry."""
        # No registry passed
        mgr = ABSSocketManager(self.db, self.sync, user_client_registry=None)
        targets = mgr._listener_targets()
        self.assertEqual(targets, [(None, "http://abs.local", "admin-token")])

    def test_global_target_fail_open_when_list_users_yields_nobody(self):
        """Fail-open: global target returned when list_users yields no active users."""
        self.db.list_users.return_value = []
        registry = MagicMock()
        mgr = ABSSocketManager(self.db, self.sync, user_client_registry=registry)
        targets = mgr._listener_targets()
        self.assertEqual(targets, [(None, "http://abs.local", "admin-token")])

    def test_global_target_fail_open_when_no_primary_admin(self):
        """Fail-open: global target returned when no user is the primary admin."""
        self.db.list_users.return_value = [_user(2), _user(3)]
        self.db.is_primary_admin.return_value = False
        registry = MagicMock()
        # Unconfigured so the per-user branch adds nothing and the assertion is
        # about the global target alone.
        registry.get_clients.return_value = _bundle("other-token", configured=False)
        mgr = ABSSocketManager(self.db, self.sync, user_client_registry=registry)
        targets = mgr._listener_targets()
        self.assertEqual(targets, [(None, "http://abs.local", "admin-token")])

    def test_global_target_fail_open_when_is_primary_admin_raises(self):
        """Fail-open: global target returned when is_primary_admin raises."""
        self.db.list_users.return_value = [_user(1)]
        self.db.is_primary_admin.side_effect = Exception("db error")
        registry = MagicMock()
        registry.get_clients.return_value = _bundle("other-token", configured=False)
        mgr = ABSSocketManager(self.db, self.sync, user_client_registry=registry)
        targets = mgr._listener_targets()
        self.assertEqual(targets, [(None, "http://abs.local", "admin-token")])

    def test_per_user_listener_still_works_when_primary_admin_abs_disabled(self):
        """Constraint test: primary admin ABS off does NOT disable other users' listeners.
        
        With the primary admin's ABS switched off, a DIFFERENT active user who has
        their own distinct token and an enabled ABS client STILL gets their per-user
        listener. This pins that one admin's choice does not disable anyone else.
        """
        # Primary admin (user 1) has ABS disabled
        # User 2 has their own ABS token and ABS enabled
        self.db.list_users.return_value = [_user(1), _user(2)]
        self.db.is_primary_admin.side_effect = lambda uid: uid == 1
        registry = MagicMock()
        # Primary admin bundle: ABS disabled
        # User 2 bundle: ABS enabled with distinct token
        def get_clients(user_id):
            if user_id == 1:
                return _bundle("admin-token", configured=False)
            return _bundle("caitlin-token", configured=True)
        registry.get_clients.side_effect = get_clients

        mgr = ABSSocketManager(self.db, self.sync, user_client_registry=registry)
        targets = mgr._listener_targets()

        # Global target should be skipped (primary admin ABS off)
        # But user 2 should still get their listener
        self.assertNotIn((None, "http://abs.local", "admin-token"), targets)
        self.assertIn((2, "http://abs.local", "caitlin-token"), targets)
        self.assertEqual(len(targets), 1)


class TestABSSocketManagerSupervise(unittest.TestCase):
    """Tests for the _supervise method with ABS_ENABLED gating."""

    def setUp(self):
        self.env = patch.dict(os.environ, {"ABS_SERVER": "http://abs.local", "ABS_KEY": "admin-token"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.db = MagicMock()
        self.sync = MagicMock()

    def test_supervise_returns_when_scope_definitively_disabled(self):
        """_supervise returns without constructing a listener when scope is definitively disabled."""
        mgr = ABSSocketManager(self.db, self.sync, user_client_registry=None)
        mgr._restart_base_secs = 0
        mgr._restart_max_secs = 0
        mgr._healthy_session_secs = 0

        # Mock _is_scope_enabled to return False (definitively disabled)
        mgr._is_scope_enabled = MagicMock(return_value=False)

        with patch("src.services.abs_socket_manager.ABSSocketListener") as MockListener:
            mgr._supervise(None, "http://abs.local", "admin-token", "global")

        # Listener should never be constructed
        MockListener.assert_not_called()

    def test_supervise_runs_normally_when_enable_check_raises(self):
        """_supervise still runs normally when the enable check raises (fail-open)."""
        mgr = ABSSocketManager(self.db, self.sync, user_client_registry=None)
        mgr._restart_base_secs = 0
        mgr._restart_max_secs = 0
        mgr._healthy_session_secs = 0
        starts = {"n": 0}
        stops = {"n": 0}

        # Mock _is_scope_enabled to raise an exception
        mgr._is_scope_enabled = MagicMock(side_effect=Exception("registry error"))

        class _FakeListener:
            def __init__(self, **kwargs):
                pass

            def start(self_inner):
                starts["n"] += 1
                if starts["n"] >= 2:
                    mgr._stop_event.set()

            def stop(self_inner):
                stops["n"] += 1

        with patch("src.services.abs_socket_manager.ABSSocketListener", _FakeListener):
            mgr._supervise(None, "http://abs.local", "admin-token", "global")

        # Should still run (fail-open) - listener constructed and started
        self.assertEqual(starts["n"], 2)
        self.assertEqual(stops["n"], 2)

    def test_supervise_stops_on_runtime_toggle_off(self):
        """_supervise stops when scope becomes disabled during runtime (second iteration)."""
        mgr = ABSSocketManager(self.db, self.sync, user_client_registry=None)
        mgr._restart_base_secs = 0
        mgr._restart_max_secs = 0
        mgr._healthy_session_secs = 0
        starts = {"n": 0}
        stops = {"n": 0}

        # First call returns True (enabled), second returns False (disabled)
        call_count = {"n": 0}
        def is_scope_enabled(user_id):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return True
            return False

        mgr._is_scope_enabled = MagicMock(side_effect=is_scope_enabled)

        class _FakeListener:
            def __init__(self, **kwargs):
                pass

            def start(self_inner):
                starts["n"] += 1
                # Don't set stop_event - we want the loop to continue and hit the second check
                if starts["n"] >= 1:
                    # This simulates the listener exiting normally
                    pass

            def stop(self_inner):
                stops["n"] += 1

        with patch("src.services.abs_socket_manager.ABSSocketListener", _FakeListener):
            mgr._supervise(None, "http://abs.local", "admin-token", "global")

        # First iteration: listener constructed and started (enabled)
        # Second iteration: check returns False, returns without constructing listener
        self.assertEqual(starts["n"], 1)
        self.assertEqual(stops["n"], 1)
        # The helper should have been called twice
        self.assertEqual(mgr._is_scope_enabled.call_count, 2)

    def test_stop_user_disconnects_and_never_restarts_listener(self):
        """Deleting an account mid-session stops its listener without the restart backoff."""
        mgr = ABSSocketManager(self.db, self.sync, user_client_registry=None)
        mgr._is_scope_enabled = MagicMock(return_value=True)
        mgr._stop_event.wait = MagicMock()
        events = []

        class _FakeListener:
            def __init__(self, **kwargs):
                pass

            def start(self_inner):
                events.append("start")
                mgr.stop_user(7)

            def stop(self_inner):
                events.append("stop")

        with patch("src.services.abs_socket_manager.ABSSocketListener", _FakeListener):
            mgr._supervise(7, "http://abs.local", "reader-token", "user 7")

        self.assertEqual(events, ["start", "stop", "stop"])
        self.assertNotIn("user 7", mgr._current_listeners)
        mgr._stop_event.wait.assert_not_called()

    def test_supervise_never_starts_a_retired_scope(self):
        mgr = ABSSocketManager(self.db, self.sync, user_client_registry=None)
        mgr._is_scope_enabled = MagicMock(return_value=True)
        mgr.stop_user(9)

        with patch("src.services.abs_socket_manager.ABSSocketListener") as MockListener:
            MockListener.return_value.start.side_effect = lambda: mgr._stop_event.set()
            mgr._supervise(9, "http://abs.local", "reader-token", "user 9")

        MockListener.return_value.start.assert_not_called()


class TestABSSocketManagerStart(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"ABS_SERVER": "http://abs.local", "ABS_KEY": "admin-token"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.db = MagicMock()
        self.sync = MagicMock()

    def test_start_supervises_one_thread_per_target(self):
        """start() launches one supervised thread per target (global + user 2);
        listeners are constructed inside the supervisor, not in start()."""
        self.db.list_users.return_value = [_user(2)]
        registry = MagicMock()
        registry.get_clients.return_value = _bundle("caitlin-token")
        mgr = ABSSocketManager(self.db, self.sync, user_client_registry=registry)

        with patch("src.services.abs_socket_manager.threading.Thread") as MockThread:
            mgr.start()

        self.assertEqual(MockThread.call_count, 2)
        for c in MockThread.call_args_list:
            self.assertEqual(c.kwargs["target"], mgr._supervise)
        supervised_user_ids = {c.kwargs["args"][0] for c in MockThread.call_args_list}
        self.assertEqual(supervised_user_ids, {None, 2})

    def test_supervise_restarts_listener_until_stopped(self):
        """A listener that exits is re-created and restarted; the loop ends when
        stop() is signalled — the core fix for the engineio teardown death."""
        mgr = ABSSocketManager(self.db, self.sync, user_client_registry=None)
        mgr._restart_base_secs = 0
        mgr._restart_max_secs = 0
        mgr._healthy_session_secs = 0
        starts = {"n": 0}
        stops = {"n": 0}
        built = []

        class _FakeListener:
            def __init__(self, **kwargs):
                built.append(kwargs)

            def start(self_inner):
                starts["n"] += 1
                if starts["n"] >= 3:
                    mgr._stop_event.set()  # stop after the 3rd (re)start

            def stop(self_inner):
                stops["n"] += 1

        with patch("src.services.abs_socket_manager.ABSSocketListener", _FakeListener):
            mgr._supervise(None, "http://abs.local", "admin-token", "global")

        self.assertEqual(starts["n"], 3)   # initial + 2 restarts
        self.assertEqual(stops["n"], 3)    # every old debounce loop is stopped
        self.assertEqual(len(built), 3)    # a fresh listener each iteration
        self.assertTrue(all(b["user_id"] is None for b in built))

    def test_stop_signals_event_and_disconnects_current_listeners(self):
        """stop() sets the stop event and disconnects the running listener(s)."""
        mgr = ABSSocketManager(self.db, self.sync, user_client_registry=None)
        listener = MagicMock()
        mgr._current_listeners["global"] = listener

        mgr.stop()

        self.assertTrue(mgr._stop_event.is_set())
        listener.stop.assert_called_once()

    def test_start_no_targets_logs_and_returns(self):
        """With no token at all, start() launches nothing."""
        with patch.dict(os.environ, {"ABS_KEY": ""}):
            mgr = ABSSocketManager(self.db, self.sync, user_client_registry=None)
            with patch("src.services.abs_socket_manager.ABSSocketListener") as MockListener:
                mgr.start()
            MockListener.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class TestABSSocketManagerStartDiagnostics(unittest.TestCase):
    """`start()` must not blame a missing token when one is configured."""

    def setUp(self):
        self.db = MagicMock()
        self.sync = MagicMock()
        self.db.is_primary_admin.return_value = False

    def _manager_with_no_targets(self):
        mgr = ABSSocketManager(self.db, self.sync, user_client_registry=None)
        mgr._listener_targets = MagicMock(return_value=[])
        return mgr

    def test_disabled_scopes_do_not_report_a_missing_token(self):
        mgr = self._manager_with_no_targets()
        with patch.dict(os.environ, {"ABS_KEY": "admin-token"}):
            with self.assertLogs("src.services.abs_socket_manager", level="INFO") as logs:
                mgr.start()
        joined = "\n".join(logs.output)
        self.assertIn("switched off for every configured scope", joined)
        self.assertNotIn("no configured ABS token found", joined)

    def test_genuinely_missing_token_keeps_the_original_warning(self):
        mgr = self._manager_with_no_targets()
        with patch.dict(os.environ, {"ABS_KEY": ""}):
            with self.assertLogs("src.services.abs_socket_manager", level="WARNING") as logs:
                mgr.start()
        self.assertIn(
            "ABS Socket.IO: no configured ABS token found — no listeners started",
            "\n".join(logs.output),
        )
