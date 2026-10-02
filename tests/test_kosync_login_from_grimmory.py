import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

import src.web_server as web_server
from src.api.booklore_client import BookloreClient


class MirrorTestCase(unittest.TestCase):
    def setUp(self):
        self._saved_db = web_server.database_service
        self._saved_container = web_server.container
        self._saved_setting = os.environ.get("KOSYNC_CREDENTIALS_FROM_GRIMMORY")
        os.environ["KOSYNC_CREDENTIALS_FROM_GRIMMORY"] = "true"

        self.db = MagicMock()
        self.db.list_users.return_value = [SimpleNamespace(id=7, username="reader@example.com", active=1)]
        self.db.get_user_credentials.return_value = {"KOSYNC_USER": "old", "KOSYNC_KEY": "oldpw"}
        web_server.database_service = self.db

        self.booklore = MagicMock()
        self.booklore.is_configured.return_value = True
        self.booklore.get_koreader_sync_login.return_value = ("reader@example.com", "abc123def456")
        self.registry = MagicMock()
        self.registry.get_clients.return_value = SimpleNamespace(booklore_client=self.booklore)
        web_server.container = SimpleNamespace(user_client_registry=lambda: self.registry)

    def tearDown(self):
        web_server.database_service = self._saved_db
        web_server.container = self._saved_container
        if self._saved_setting is None:
            os.environ.pop("KOSYNC_CREDENTIALS_FROM_GRIMMORY", None)
        else:
            os.environ["KOSYNC_CREDENTIALS_FROM_GRIMMORY"] = self._saved_setting


class TestMirror(MirrorTestCase):
    def test_off_does_nothing(self):
        os.environ["KOSYNC_CREDENTIALS_FROM_GRIMMORY"] = "false"
        web_server._mirror_kosync_logins_from_grimmory()
        self.db.set_user_credential.assert_not_called()

    def test_copies_a_changed_login_and_invalidates_the_bundle(self):
        updated = web_server._mirror_kosync_logins_from_grimmory()
        calls = {(c.args[1], c.args[2]) for c in self.db.set_user_credential.call_args_list}
        self.assertEqual({("KOSYNC_USER", "reader@example.com"), ("KOSYNC_KEY", "abc123def456")}, calls)
        self.registry.invalidate.assert_called_once_with(7)
        self.assertEqual(1, updated)

    def test_unchanged_login_writes_nothing(self):
        self.db.get_user_credentials.return_value = {"KOSYNC_USER": "reader@example.com", "KOSYNC_KEY": "abc123def456"}
        web_server._mirror_kosync_logins_from_grimmory()
        self.db.set_user_credential.assert_not_called()
        self.registry.invalidate.assert_not_called()

    def test_reader_without_grimmory_login_is_skipped(self):
        self.booklore.get_koreader_sync_login.return_value = None
        web_server._mirror_kosync_logins_from_grimmory()
        self.db.set_user_credential.assert_not_called()

    def test_unconfigured_grimmory_is_skipped(self):
        self.booklore.is_configured.return_value = False
        web_server._mirror_kosync_logins_from_grimmory()
        self.booklore.get_koreader_sync_login.assert_not_called()

    def test_inactive_users_are_skipped(self):
        self.db.list_users.return_value = [SimpleNamespace(id=8, username="gone@example.com", active=0)]
        web_server._mirror_kosync_logins_from_grimmory()
        self.registry.get_clients.assert_not_called()

    def test_one_failing_user_does_not_stop_the_others(self):
        self.db.list_users.return_value = [
            SimpleNamespace(id=1, username="a@example.com", active=1),
            SimpleNamespace(id=2, username="b@example.com", active=1),
        ]
        self.registry.get_clients.side_effect = [RuntimeError("boom"), SimpleNamespace(booklore_client=self.booklore)]
        updated = web_server._mirror_kosync_logins_from_grimmory()
        self.assertEqual(1, updated)


class TestClientLogin(unittest.TestCase):
    def _client(self, response):
        client = BookloreClient.__new__(BookloreClient)
        client._make_request = MagicMock(return_value=response)
        client._parse_json_response = lambda resp, label: resp.json()
        return client

    def test_returns_username_and_password(self):
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"username": "reader@example.com", "password": "abc123def456", "syncEnabled": True}
        self.assertEqual(("reader@example.com", "abc123def456"), self._client(resp).get_koreader_sync_login())

    def test_missing_login_is_none(self):
        self.assertIsNone(self._client(MagicMock(status_code=404)).get_koreader_sync_login())
        self.assertIsNone(self._client(None).get_koreader_sync_login())

    def test_blank_fields_are_none(self):
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"username": "", "password": "x"}
        self.assertIsNone(self._client(resp).get_koreader_sync_login())


if __name__ == "__main__":
    unittest.main()
