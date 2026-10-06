import logging
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import src.web_server as web_server
from src.api import kosync_server
from src.api.booklore_client import BookloreClient
from src.db.database_service import DatabaseService
from src.utils import secret_store
from src.utils.kosync_headers import hash_kosync_key
from src.utils.logging_utils import get_persistent_condition_logger
from src.utils.user_config import PER_USER_CREDENTIAL_KEYS, PER_USER_FIELD_GROUPS

SETTING = "KOSYNC_CREDENTIALS_FROM_GRIMMORY"
MARKER = web_server.KOSYNC_MIRRORED_LOGIN_KEY
GRIMMORY_LOGIN = ("reader@example.com", "abc123def456")


class FakeDatabase:
    def __init__(self, users):
        self.users = users
        self.creds = {}
        self.default_user_id = None

    def list_users(self):
        return list(self.users)

    def get_user_credentials(self, user_id):
        return dict(self.creds.get(user_id, {}))

    def set_user_credential(self, user_id, key, value):
        self.creds.setdefault(user_id, {})[key] = value

    def set_user_credentials(self, user_id, values):
        self.creds.setdefault(user_id, {}).update(values)

    def _default_user_id(self):
        return self.default_user_id


def make_user(user_id, username, active=1):
    return SimpleNamespace(id=user_id, username=username, active=active)


def grimmory_client(login=GRIMMORY_LOGIN, configured=True):
    client = MagicMock()
    client.is_configured.return_value = configured
    client.get_koreader_sync_login.return_value = login
    return client


def make_registry(clients):
    registry = MagicMock()
    registry.get_clients.side_effect = lambda user_id: SimpleNamespace(booklore_client=clients[user_id])
    return registry


@contextmanager
def mirror_globals(database, registry):
    saved_database = web_server.database_service
    saved_container = web_server.container
    get_persistent_condition_logger().reset()
    try:
        web_server.database_service = database
        web_server.container = SimpleNamespace(user_client_registry=lambda: registry)
        yield
    finally:
        web_server.database_service = saved_database
        web_server.container = saved_container
        get_persistent_condition_logger().reset()


class MirrorTestCase(unittest.TestCase):
    def setUp(self):
        self.db = FakeDatabase([make_user(7, "reader")])
        self.clients = {7: grimmory_client()}
        self.registry = make_registry(self.clients)
        self.enterContext(patch.dict(os.environ, {SETTING: "true"}))
        os.environ.pop("KOSYNC_USER", None)
        self.enterContext(mirror_globals(self.db, self.registry))

    def mirror(self):
        return web_server._mirror_kosync_logins_from_grimmory()

    def login(self, user_id):
        creds = self.db.creds.get(user_id, {})
        return creds.get("KOSYNC_USER"), creds.get("KOSYNC_KEY")

    def warnings(self, captured, text):
        return [r for r in captured.records if r.levelno == logging.WARNING and text in r.getMessage()]


class TestSetting(MirrorTestCase):
    def test_off_does_nothing(self):
        with patch.dict(os.environ, {SETTING: "false"}):
            self.assertIsNone(self.mirror())
        self.assertEqual({}, self.db.creds)
        self.clients[7].get_koreader_sync_login.assert_not_called()

    def test_true_and_on_both_enable_it(self):
        for value in ("true", "on"):
            with self.subTest(value=value), patch.dict(os.environ, {SETTING: value}):
                self.db.creds.clear()
                self.assertEqual(1, self.mirror())
                self.assertEqual(GRIMMORY_LOGIN, self.login(7))


class TestExistingLogins(MirrorTestCase):
    def test_reader_without_a_login_takes_the_grimmory_one(self):
        self.assertEqual(1, self.mirror())
        self.assertEqual(GRIMMORY_LOGIN, self.login(7))
        self.registry.invalidate.assert_called_once_with(7)

    def test_username_without_a_password_is_not_a_login(self):
        self.db.creds[7] = {"KOSYNC_USER": "kobo"}
        self.assertEqual(1, self.mirror())
        self.assertEqual(GRIMMORY_LOGIN, self.login(7))

    def test_login_set_in_bookbridge_is_kept(self):
        self.db.creds[7] = {"KOSYNC_USER": "kobo", "KOSYNC_KEY": "kobo-pw"}
        self.assertEqual(0, self.mirror())
        self.assertEqual({"KOSYNC_USER": "kobo", "KOSYNC_KEY": "kobo-pw"}, self.db.creds[7])
        self.registry.invalidate.assert_not_called()

    def test_kept_login_is_warned_about_once_until_it_is_cleared(self):
        self.db.creds[7] = {"KOSYNC_USER": "kobo", "KOSYNC_KEY": "kobo-pw"}
        with self.assertLogs("src.web_server", level="DEBUG") as captured:
            for _ in range(3):
                self.mirror()
            self.db.creds[7]["KOSYNC_USER"] = ""
            self.assertEqual(1, self.mirror())
        kept = self.warnings(captured, "is kept")
        self.assertEqual(1, len(kept))
        self.assertIn("'reader'", kept[0].getMessage())
        self.assertTrue(any("now follows Grimmory" in r.getMessage() for r in captured.records))
        self.assertEqual(GRIMMORY_LOGIN, self.login(7))

    def test_mirrored_login_follows_changes_in_grimmory(self):
        self.mirror()
        self.clients[7].get_koreader_sync_login.return_value = ("renamed@example.com", "rotated")
        self.assertEqual(1, self.mirror())
        self.assertEqual(("renamed@example.com", "rotated"), self.login(7))
        self.clients[7].get_koreader_sync_login.return_value = ("renamed@example.com", "rotated-again")
        self.assertEqual(1, self.mirror())
        self.assertEqual(("renamed@example.com", "rotated-again"), self.login(7))

    def test_login_matching_grimmory_is_adopted_silently_then_follows(self):
        self.db.creds[7] = {"KOSYNC_USER": GRIMMORY_LOGIN[0], "KOSYNC_KEY": GRIMMORY_LOGIN[1]}
        with self.assertNoLogs("src.web_server", level="INFO"):
            self.assertEqual(0, self.mirror())
        self.assertEqual(GRIMMORY_LOGIN, self.login(7))
        self.registry.invalidate.assert_not_called()
        self.clients[7].get_koreader_sync_login.return_value = (GRIMMORY_LOGIN[0], "rotated")
        self.assertEqual(1, self.mirror())
        self.assertEqual((GRIMMORY_LOGIN[0], "rotated"), self.login(7))

    def test_unchanged_mirrored_login_writes_nothing(self):
        self.mirror()
        before = dict(self.db.creds[7])
        self.registry.invalidate.reset_mock()
        self.assertEqual(0, self.mirror())
        self.assertEqual(before, self.db.creds[7])
        self.registry.invalidate.assert_not_called()

    def test_login_edited_in_bookbridge_after_mirroring_is_kept(self):
        self.mirror()
        self.db.creds[7].update({"KOSYNC_USER": "kobo", "KOSYNC_KEY": "kobo-pw"})
        self.assertEqual(0, self.mirror())
        self.assertEqual(("kobo", "kobo-pw"), self.login(7))


class TestCollisions(MirrorTestCase):
    def setUp(self):
        super().setUp()
        self.db.users[:] = [make_user(1, "first"), make_user(2, "second")]
        self.clients.update({1: grimmory_client(), 2: grimmory_client()})

    def test_two_users_on_one_grimmory_account_do_not_share_a_login(self):
        with self.assertLogs("src.web_server", level="WARNING") as captured:
            self.assertEqual(1, self.mirror())
        self.assertEqual(GRIMMORY_LOGIN, self.login(1))
        self.assertNotIn(2, self.db.creds)
        message = self.warnings(captured, "already belongs to")[0].getMessage()
        self.assertIn("'second'", message)
        self.assertIn("'first'", message)

    def test_username_another_user_set_is_skipped_whatever_its_case(self):
        self.db.creds[1] = {"KOSYNC_USER": "READER@example.com", "KOSYNC_KEY": "kobo-pw"}
        self.clients[1] = grimmory_client(configured=False)
        self.clients[2] = grimmory_client(("Reader@Example.com", "abc123def456"))
        self.assertEqual(0, self.mirror())
        self.assertNotIn(2, self.db.creds)

    def test_username_of_an_inactive_user_is_skipped(self):
        self.db.users[0].active = 0
        self.db.creds[1] = {"KOSYNC_USER": GRIMMORY_LOGIN[0], "KOSYNC_KEY": "kobo-pw"}
        self.assertEqual(0, self.mirror())
        self.assertNotIn(2, self.db.creds)

    def test_global_username_belongs_to_the_default_user(self):
        self.db.default_user_id = 1
        self.clients[1] = grimmory_client(configured=False)
        with patch.dict(os.environ, {"KOSYNC_USER": "Reader@Example.com"}):
            self.assertEqual(0, self.mirror())
            self.assertNotIn(2, self.db.creds)
            self.clients[1] = grimmory_client()
            self.assertEqual(1, self.mirror())
        self.assertEqual(GRIMMORY_LOGIN, self.login(1))
        self.assertNotIn(2, self.db.creds)

    def test_collision_is_warned_about_once_until_it_is_gone(self):
        with self.assertLogs("src.web_server", level="DEBUG") as captured:
            for _ in range(3):
                self.mirror()
            self.clients[1].get_koreader_sync_login.return_value = ("first@example.com", "first-pw")
            self.mirror()
            self.assertEqual(1, self.mirror())
        self.assertEqual(1, len(self.warnings(captured, "already belongs to")))
        self.assertTrue(any("no longer belongs" in r.getMessage() for r in captured.records))
        self.assertEqual(GRIMMORY_LOGIN, self.login(2))


class TestSkipsAndFailures(MirrorTestCase):
    def test_reader_without_grimmory_login_is_skipped(self):
        self.clients[7].get_koreader_sync_login.return_value = None
        self.assertEqual(0, self.mirror())
        self.assertEqual({}, self.db.creds)

    def test_unconfigured_grimmory_is_skipped(self):
        self.clients[7].is_configured.return_value = False
        self.assertEqual(0, self.mirror())
        self.clients[7].get_koreader_sync_login.assert_not_called()

    def test_inactive_users_are_skipped(self):
        self.db.users[0].active = 0
        self.assertEqual(0, self.mirror())
        self.registry.get_clients.assert_not_called()

    def test_one_failing_user_does_not_stop_the_others(self):
        self.db.users.append(make_user(8, "other"))
        self.clients[7].get_koreader_sync_login.side_effect = RuntimeError("boom")
        self.clients[8] = grimmory_client(("other@example.com", "other-pw"))
        with self.assertLogs("src.web_server", level="WARNING") as captured:
            self.assertEqual(1, self.mirror())
        self.assertEqual(("other@example.com", "other-pw"), self.login(8))
        failures = self.warnings(captured, "failed for user 7")
        self.assertEqual(1, len(failures))
        self.assertIsNotNone(failures[0].exc_info)

    def test_repeating_failure_is_warned_about_once_until_it_recovers(self):
        self.clients[7].get_koreader_sync_login.side_effect = RuntimeError("boom")
        with self.assertLogs("src.web_server", level="DEBUG") as captured:
            for _ in range(3):
                self.mirror()
            self.clients[7].get_koreader_sync_login.side_effect = None
            self.assertEqual(1, self.mirror())
        self.assertEqual(1, len(self.warnings(captured, "failed for user 7")))
        self.assertTrue(any("works again for user 7" in r.getMessage() for r in captured.records))

    def test_unlistable_users_are_warned_about_once_with_the_traceback(self):
        with patch.object(self.db, "list_users", side_effect=RuntimeError("locked")), \
                self.assertLogs("src.web_server", level="DEBUG") as captured:
            for _ in range(3):
                self.assertIsNone(self.mirror())
        failures = self.warnings(captured, "could not list users")
        self.assertEqual(1, len(failures))
        self.assertIsNotNone(failures[0].exc_info)


class TestMirroredLoginMarker(MirrorTestCase):
    def test_marker_is_not_an_editable_field_or_a_client_credential(self):
        editable = {key for _group, fields in PER_USER_FIELD_GROUPS for key, _label, _type in fields}
        self.assertNotIn(MARKER, editable)
        self.assertNotIn(MARKER, PER_USER_CREDENTIAL_KEYS)

    def test_marker_is_a_fingerprint_kept_with_the_secrets(self):
        self.mirror()
        self.assertNotIn(GRIMMORY_LOGIN[1], self.db.creds[7][MARKER])
        self.assertIn(MARKER, secret_store.secret_keys())


class TestAgainstRealDatabase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.enterContext(patch.dict(os.environ, {"DATA_DIR": self.tmpdir, SETTING: "on"}))
        for key in ("BOOKBRIDGE_SECRET_KEY", "BOOKBRIDGE_SECRET_KEY_FILE", "KOSYNC_USER", "KOSYNC_KEY"):
            os.environ.pop(key, None)
        secret_store.reset_cache()
        self.addCleanup(secret_store.reset_cache)
        self.db_path = str(Path(self.tmpdir) / "database.db")
        self.svc = DatabaseService(self.db_path)
        self.addCleanup(self.svc.db_manager.close)
        self.first = self.svc.create_user("first", "pw-for-login", role="admin")
        self.second = self.svc.create_user("second", "pw-for-login")
        clients = {self.first.id: grimmory_client(), self.second.id: grimmory_client()}
        self.enterContext(mirror_globals(self.svc, make_registry(clients)))

    def authenticate(self, username, key):
        saved = kosync_server._database_service
        try:
            kosync_server._database_service = self.svc
            return kosync_server.authenticate_kosync(username, key)
        finally:
            kosync_server._database_service = saved

    def test_device_sending_the_md5_of_the_grimmory_password_authenticates(self):
        self.assertEqual(1, web_server._mirror_kosync_logins_from_grimmory())
        device_key = hash_kosync_key(GRIMMORY_LOGIN[1])
        self.assertEqual((True, self.first.id), self.authenticate(GRIMMORY_LOGIN[0], device_key))
        self.assertEqual((False, None), self.authenticate(GRIMMORY_LOGIN[0], hash_kosync_key("wrong")))

    def test_second_user_on_the_same_grimmory_account_gets_no_login(self):
        web_server._mirror_kosync_logins_from_grimmory()
        self.assertEqual(GRIMMORY_LOGIN[0], self.svc.get_user_credential(self.first.id, "KOSYNC_USER"))
        self.assertIsNone(self.svc.get_user_credential(self.second.id, "KOSYNC_USER"))

    def test_fingerprint_is_encrypted_at_rest(self):
        web_server._mirror_kosync_logins_from_grimmory()
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute("SELECT value FROM user_credentials WHERE key = ?", (MARKER,)).fetchall()
        finally:
            conn.close()
        self.assertEqual(1, len(rows))
        self.assertTrue(secret_store.is_encrypted(rows[0][0]))

    def test_interrupted_rotation_rolls_back_and_can_retry(self):
        from sqlalchemy import event

        registry = make_registry({self.first.id: grimmory_client(), self.second.id: grimmory_client()})
        with mirror_globals(self.svc, registry):
            web_server._mirror_kosync_logins_from_grimmory()
            original = self.svc.get_user_credentials(self.first.id)
            client = registry.get_clients(self.first.id).booklore_client
            client.get_koreader_sync_login.return_value = ("rotated-reader", "rotated-password")
            registry.invalidate.reset_mock()

            def fail_password(conn, cursor, statement, parameters, context, executemany):
                if statement.startswith("INSERT INTO user_credentials") and "KOSYNC_KEY" in parameters:
                    raise RuntimeError("temporary database write failure")

            engine = self.svc.db_manager.engine
            event.listen(engine, "before_cursor_execute", fail_password)
            try:
                web_server._mirror_kosync_logins_from_grimmory()
            finally:
                event.remove(engine, "before_cursor_execute", fail_password)
            self.assertEqual(original, self.svc.get_user_credentials(self.first.id))
            registry.invalidate.assert_not_called()
            self.assertEqual(1, web_server._mirror_kosync_logins_from_grimmory())
            self.assertEqual((True, self.first.id), self.authenticate(
                "rotated-reader", hash_kosync_key("rotated-password")))
            registry.invalidate.assert_called_once_with(self.first.id)


class TestClientLogin(unittest.TestCase):
    def _client(self, response):
        client = BookloreClient.__new__(BookloreClient)
        client._make_request = MagicMock(return_value=response)
        client._parse_json_response = lambda resp, label: resp.json()
        return client

    def test_returns_username_and_plaintext_password(self):
        resp = MagicMock(status_code=200)
        resp.json.return_value = {
            "username": "reader@example.com",
            "password": "abc123def456",
            "passwordMD5": hash_kosync_key("abc123def456"),
            "syncEnabled": True,
        }
        self.assertEqual(GRIMMORY_LOGIN, self._client(resp).get_koreader_sync_login())

    def test_missing_login_is_none(self):
        self.assertIsNone(self._client(MagicMock(status_code=404)).get_koreader_sync_login())
        self.assertIsNone(self._client(None).get_koreader_sync_login())

    def test_blank_fields_are_none(self):
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"username": "", "password": "x"}
        self.assertIsNone(self._client(resp).get_koreader_sync_login())


if __name__ == "__main__":
    unittest.main()
