"""Account lifecycle hardening: usernames, credential writes, disable handling."""
import logging
import os
import shutil
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, call, patch

from sqlalchemy import event

from src.db.database_service import DatabaseService
from src.db.models import Book
from src.services.abs_socket_manager import ABSSocketManager
from src.sync_manager import SyncManager


class DbTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = DatabaseService(os.path.join(self.tmp, "lifecycle.db"))


class UsernameTests(DbTestCase):
    def test_non_ascii_username_is_found_in_any_case(self):
        user = self.db.create_user("Émile", "pw")
        self.assertEqual(self.db.get_user_by_username("émile").id, user.id)
        self.assertEqual(self.db.get_user_by_username("ÉMILE").id, user.id)
        self.assertIsNotNone(self.db.verify_user_credentials("émile", "pw"))

    def test_decomposed_and_composed_forms_are_the_same_account(self):
        self.db.create_user("émile", "pw")
        with self.assertRaises(ValueError):
            self.db.create_user("émile", "pw")

    def test_duplicate_ignoring_unicode_case_is_refused(self):
        self.db.create_user("Émile", "pw")
        with self.assertRaises(ValueError):
            self.db.create_user("émile", "pw")

    def test_rename_cannot_collide_with_a_unicode_case_variant(self):
        self.db.create_user("Émile", "pw")
        other = self.db.create_user("other", "pw")
        ok, error = self.db.set_username(other.id, "éMILE")
        self.assertFalse(ok)
        self.assertIn("already taken", error)
        self.assertEqual(self.db.set_username(other.id, "Other"), (True, None))

    def test_concurrent_creates_of_case_variants_make_one_account(self):
        """Both creators reach the INSERT together unless BEGIN IMMEDIATE serializes them."""
        barrier = threading.Barrier(2)

        def meet_at_insert(conn, cursor, statement, parameters, context, executemany):
            if statement.startswith("INSERT INTO users"):
                try:
                    barrier.wait(timeout=1)
                except threading.BrokenBarrierError:
                    pass

        engine = self.db.db_manager.engine
        event.listen(engine, "before_cursor_execute", meet_at_insert)
        self.addCleanup(event.remove, engine, "before_cursor_execute", meet_at_insert)

        def create(name):
            try:
                self.db.create_user(name, "pw")
                return True
            except ValueError:
                return False

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(create, ["Émile", "émile"]))
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(len(self.db.list_users()), 1)


class CredentialWriteTests(DbTestCase):
    def test_bulk_writer_ignores_a_deleted_account(self):
        admin = self.db.create_user("admin", "pw", role="admin")
        reader = self.db.create_user("reader", "pw")
        self.assertTrue(self.db.delete_user(reader.id))
        self.assertFalse(self.db.set_user_credentials(reader.id, {"KOSYNC_USER": "ghost"}))
        self.assertIsNone(self.db.set_user_credential(reader.id, "KOSYNC_USER", "ghost"))
        self.assertEqual(self.db.get_user_credentials(reader.id), {})
        self.assertTrue(self.db.set_user_credentials(admin.id, {"KOSYNC_USER": "ok"}))

    def test_guard_and_write_share_a_transaction(self):
        """A delete landing between the existence check and the write must not leave rows behind."""
        self.db.create_user("admin", "pw", role="admin")
        reader = self.db.create_user("reader", "pw")
        in_write = threading.Event()
        release = threading.Event()

        def pause_on_insert(conn, cursor, statement, parameters, context, executemany):
            if statement.startswith("INSERT INTO user_credentials"):
                in_write.set()
                release.wait(timeout=2)

        engine = self.db.db_manager.engine
        event.listen(engine, "before_cursor_execute", pause_on_insert)
        self.addCleanup(event.remove, engine, "before_cursor_execute", pause_on_insert)
        with ThreadPoolExecutor(max_workers=2) as pool:
            writer = pool.submit(self.db.set_user_credentials, reader.id, {"KOSYNC_USER": "late"})
            self.assertTrue(in_write.wait(timeout=2))
            deleter = pool.submit(self.db.delete_user, reader.id)
            release.set()
            writer.result(timeout=10)
            deleter.result(timeout=10)
        self.assertEqual(self.db.get_user_credentials(reader.id), {})

    def test_readest_tokens_from_a_replaced_identity_are_dropped(self):
        reader = self.db.create_user("reader", "pw")
        self.db.set_user_credentials(reader.id, {"READEST_EMAIL": "old@x.invalid", "READEST_PASSWORD": "old"})
        # The identity is reset while a client authenticated as the old one is in flight.
        self.db.set_user_credentials(reader.id, {"READEST_EMAIL": "new@x.invalid", "READEST_PASSWORD": "new"})
        tokens = {"READEST_ACCESS_TOKEN": "stale", "READEST_REFRESH_TOKEN": "stale", "READEST_TOKEN_EXPIRES_AT": "1"}
        self.assertFalse(self.db.persist_readest_tokens(reader.id, tokens, "old@x.invalid", "old"))
        self.assertFalse(self.db.get_user_credential(reader.id, "READEST_ACCESS_TOKEN"))
        self.assertTrue(self.db.persist_readest_tokens(reader.id, tokens, "new@x.invalid", "new"))
        self.assertEqual(self.db.get_user_credential(reader.id, "READEST_ACCESS_TOKEN"), "stale")

    def test_readest_client_persists_with_the_identity_it_authenticated_with(self):
        from src.api.readest_client import ReadestClient

        db = MagicMock()
        client = ReadestClient({"READEST_EMAIL": "a@b.invalid", "READEST_PASSWORD": "pw"}, db, 4)
        client._persist_tokens({"access_token": "t", "refresh_token": "r", "expires_in": 60},
                               identity=("login@b.invalid", "typed"))
        self.assertEqual(db.persist_readest_tokens.call_args.args[2:], ("login@b.invalid", "typed"))

    def test_readest_client_discards_tokens_the_database_refused(self):
        from src.api.readest_client import ReadestClient

        db = MagicMock()
        db.persist_readest_tokens.return_value = False
        creds = {"READEST_EMAIL": "a@b.invalid", "READEST_PASSWORD": "pw"}
        ReadestClient(creds, db, 4)._persist_tokens({"access_token": "t", "refresh_token": "r"})
        self.assertNotIn("READEST_ACCESS_TOKEN", creds)


class AccountPolicyTests(DbTestCase):
    def test_promotion_that_would_take_over_as_primary_admin_is_refused(self):
        early = self.db.create_user("early", "pw")
        self.db.create_user("owner", "pw", role="admin")
        with self.assertRaisesRegex(ValueError, "primary admin"):
            self.db.update_user_account(early.id, role="admin")

    def test_unused_policy_bypassing_setters_are_gone(self):
        self.assertFalse(hasattr(self.db, "set_user_active"))
        self.assertFalse(hasattr(self.db, "set_user_role"))


class SocketManagerLifecycleTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"ABS_SERVER": "http://abs.local", "ABS_KEY": "admin-token"})
        env.start()
        self.addCleanup(env.stop)
        self.db = MagicMock()
        self.registry = MagicMock()
        self.mgr = ABSSocketManager(self.db, MagicMock(), user_client_registry=self.registry)

    def test_inactive_or_missing_user_is_definitively_disabled(self):
        self.db.get_user.return_value = MagicMock(active=0)
        self.assertFalse(self.mgr._is_scope_enabled(5))
        self.db.get_user.return_value = None
        self.assertFalse(self.mgr._is_scope_enabled(5))
        self.registry.get_clients.assert_not_called()

    def test_active_user_still_follows_the_abs_client(self):
        self.db.get_user.return_value = MagicMock(active=1)
        self.registry.get_clients.return_value.sync_clients = {
            "ABS": MagicMock(abs_client=MagicMock(is_configured=MagicMock(return_value=True)))
        }
        self.assertTrue(self.mgr._is_scope_enabled(5))

    def test_refresh_drops_the_connection_without_retiring_the_scope(self):
        listener = MagicMock()
        self.mgr._current_listeners["user 5"] = listener
        self.mgr._supervisors["user 5"] = MagicMock(is_alive=MagicMock(return_value=True))
        self.mgr.refresh_user(5)
        listener.stop.assert_called_once()
        self.assertNotIn("user 5", self.mgr._retired_scopes)
        self.assertNotIn("user 5", self.mgr._current_listeners)

    def test_refresh_after_disable_makes_the_supervisor_exit(self):
        self.db.get_user.return_value = MagicMock(active=0)
        with patch("src.services.abs_socket_manager.ABSSocketListener") as listener_cls:
            self.mgr._supervise(5, "http://abs.local", "tok", "user 5")
        listener_cls.assert_not_called()

    def test_refresh_starts_a_supervisor_when_a_disabled_user_is_reenabled(self):
        self.mgr._listener_targets = MagicMock(return_value=[(5, "http://abs.local", "tok")])
        self.mgr._is_scope_enabled = MagicMock(return_value=True)
        started = []
        self.mgr._spawn_supervisor = lambda uid, server, token: started.append((uid, server, token))
        self.mgr.refresh_user(5)
        self.assertEqual(started, [(5, "http://abs.local", "tok")])

    def test_refresh_does_not_start_a_second_supervisor(self):
        self.mgr._supervisors["user 5"] = MagicMock(is_alive=MagicMock(return_value=True))
        self.mgr._spawn_supervisor = MagicMock()
        self.mgr.refresh_user(5)
        self.mgr._spawn_supervisor.assert_not_called()

    def test_refresh_leaves_a_deleted_scope_retired(self):
        self.mgr.stop_user(5)
        self.mgr._spawn_supervisor = MagicMock()
        self.mgr.refresh_user(5)
        self.mgr._spawn_supervisor.assert_not_called()


class SyncCycleInactiveUserTests(unittest.TestCase):
    def _manager(self, user):
        manager = SyncManager.__new__(SyncManager)
        manager.database_service = MagicMock()
        manager.database_service.get_user.return_value = user
        manager.user_client_registry = MagicMock()
        manager._sync_cycle_internal = MagicMock()
        return manager

    def test_inactive_and_missing_users_do_not_sync(self):
        for user in (MagicMock(active=0), None):
            manager = self._manager(user)
            manager.sync_cycle(target_abs_id="book-1", user_id=5)
            manager._sync_cycle_internal.assert_not_called()
            manager.user_client_registry.get_clients.assert_not_called()


class DashboardToggleTests(DbTestCase):
    def setUp(self):
        super().setUp()
        from src import web_server

        self.web_server = web_server
        self.admin = self.db.create_user("admin", "pw", role="admin")
        self.reader = self.db.create_user("reader", "pw")
        self.socket_manager = MagicMock()
        for target, value in (("database_service", self.db), ("container", MagicMock()),
                              ("abs_socket_manager", self.socket_manager)):
            patcher = patch.object(web_server, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_disable_and_enable_refresh_the_users_abs_listener(self):
        for _ in range(2):
            message, error = self.web_server._apply_user_admin_action(
                {"action": "toggle_active", "user_id": str(self.reader.id)})
            self.assertIsNone(error)
        self.assertEqual(self.socket_manager.refresh_user.call_args_list,
                         [call(self.reader.id)] * 2)

    def test_reenable_logs_returning_user_and_creation_logs_new_user(self):
        self.db.save_book(Book(abs_id="b1", abs_title="b1", status="active"))
        self.db.update_user_account(self.reader.id, active=False)
        with patch.dict(os.environ, {"SHARE_ALL_BOOKS_WITH_ALL_USERS": "true"}), \
                self.assertLogs(self.web_server.logger, level="INFO") as logs:
            self.web_server._apply_user_admin_action({"action": "toggle_active", "user_id": str(self.reader.id)})
            fresh = self.db.create_user("fresh", "pw")
            self.web_server._share_library_with_new_user(fresh)
        text = "\n".join(logs.output)
        self.assertIn("with re-enabled user 'reader'", text)
        self.assertIn("with new user 'fresh'", text)

    def test_policy_refusal_is_the_page_error_verbatim(self):
        message, error = self.web_server._apply_user_admin_action(
            {"action": "toggle_active", "user_id": str(self.admin.id)})
        self.assertIsNone(message)
        self.assertEqual(error, "Can't disable the last active admin")
        self.socket_manager.refresh_user.assert_not_called()

    def test_saving_integrations_refreshes_the_users_abs_listener(self):
        self.web_server._after_user_integrations_saved(self.reader.id)
        self.socket_manager.refresh_user.assert_called_once_with(self.reader.id)

    def test_primary_admin_key_is_mirrored_before_the_listener_refresh(self):
        """Refreshing first would see the admin's new token differ from the global
        key and give the primary admin a second, per-user listener."""
        self.db.set_user_credential(self.admin.id, "ABS_KEY", "rotated-token")
        seen = []
        self.socket_manager.refresh_user.side_effect = lambda uid: seen.append(os.environ.get("ABS_KEY"))
        with patch.dict(os.environ, {"ABS_KEY": "old-token"}):
            self.web_server._after_user_integrations_saved(self.admin.id)
        self.assertEqual(seen, ["rotated-token"])


if __name__ == "__main__":
    unittest.main()
