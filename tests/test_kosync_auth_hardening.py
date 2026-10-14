import hmac
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from flask import Flask

import src.api.kosync_server as ks
from src.db.database_service import DatabaseService
from src.web_server import create_sync_only_app
from src.utils.kosync_headers import hash_kosync_key


class TestKosyncAuthHardening(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.svc = DatabaseService(os.path.join(self.tmp, "auth.db"))
        self._orig_db = ks._database_service
        ks._database_service = self.svc
        self._orig_env = {k: os.environ.get(k) for k in (
            "KOSYNC_USER", "KOSYNC_KEY", "KOSYNC_TRUSTED_PROXIES"
        )}
        os.environ["KOSYNC_USER"] = "reader"
        os.environ["KOSYNC_KEY"] = "right-key"
        os.environ.pop("KOSYNC_TRUSTED_PROXIES", None)
        ks._AUTH_FAILURES.clear()
        self.client = create_sync_only_app().test_client()

    def tearDown(self):
        ks._database_service = self._orig_db
        for k, v in self._orig_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        ks._AUTH_FAILURES.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _auth(self, key, forwarded=None):
        headers = {"x-auth-user": "reader", "x-auth-key": key}
        if forwarded:
            headers["X-Forwarded-For"] = forwarded
        return self.client.get("/users/auth", headers=headers).status_code

    def _data_route(self, key, forwarded=None):
        headers = {"x-auth-user": "reader", "x-auth-key": key}
        if forwarded:
            headers["X-Forwarded-For"] = forwarded
        return self.client.get("/device-sync/plugin/version", headers=headers).status_code

    def test_login_routes_refuse_after_five_failures(self):
        for name in ("auth", "login", "create"):
            with self.subTest(route=name):
                ks._AUTH_FAILURES.clear()
                if name == "auth":
                    for _ in range(5):
                        self.assertEqual(self._auth("wrong"), 401)
                    self.assertEqual(self._auth("wrong"), 429)
                    self.assertEqual(self._auth("right-key"), 429)
                else:
                    route = "/users/%s" % name
                    for _ in range(5):
                        r = self.client.post(
                            route,
                            json={"username": "reader", "password": "wrong"},
                        )
                        self.assertEqual(r.status_code, 401)
                    r = self.client.post(
                        route,
                        json={"username": "reader", "password": "wrong"},
                    )
                    self.assertEqual(r.status_code, 429)
                    self.assertEqual(
                        r.get_json(), {"message": "Too many authentication attempts"}
                    )

    def test_success_clears_the_failure_count(self):
        for _ in range(4):
            self.assertEqual(self._auth("wrong"), 401)
        self.assertEqual(self._auth("right-key"), 200)
        for _ in range(4):
            self.assertEqual(self._auth("wrong"), 401)

    def test_behind_trusted_proxy_lockout_hits_the_sender_not_the_reader(self):
        for _ in range(5):
            self.assertEqual(self._data_route("wrong", forwarded="203.0.113.9"), 401)
        self.assertEqual(self._data_route("wrong", forwarded="203.0.113.9"), 429)
        self.assertIn(
            self._data_route("right-key", forwarded="198.51.100.7"),
            (200, 404),
        )

    def test_forwarded_header_ignored_from_an_untrusted_peer(self):
        os.environ["KOSYNC_TRUSTED_PROXIES"] = "10.0.0.0/8"
        for i in range(1, 6):
            self.assertEqual(
                self._data_route("wrong", forwarded="203.0.113.%d" % i), 401
            )
        self.assertEqual(
            self._data_route("wrong", forwarded="203.0.113.99"), 429
        )

    def test_client_address_is_the_rightmost_untrusted_hop(self):
        app = Flask(__name__)
        with app.test_request_context(
            "/",
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
            headers={"X-Forwarded-For": "6.6.6.6, 203.0.113.9, 127.0.0.1"},
        ):
            self.assertEqual(ks._kosync_client_address(), "203.0.113.9")
        with app.test_request_context(
            "/",
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
        ):
            self.assertEqual(ks._kosync_client_address(), "127.0.0.1")
        with app.test_request_context(
            "/",
            environ_base={"REMOTE_ADDR": "192.0.2.50"},
            headers={"X-Forwarded-For": "203.0.113.9"},
        ):
            self.assertEqual(ks._kosync_client_address(), "192.0.2.50")
        os.environ["KOSYNC_TRUSTED_PROXIES"] = "not-an-ip, 192.0.2.50"
        try:
            with app.test_request_context(
                "/",
                environ_base={"REMOTE_ADDR": "192.0.2.50"},
                headers={"X-Forwarded-For": "203.0.113.9"},
            ):
                self.assertEqual(ks._kosync_client_address(), "203.0.113.9")
        finally:
            os.environ.pop("KOSYNC_TRUSTED_PROXIES", None)

    def test_sync_only_app_caps_request_body(self):
        body = b'{"username": "reader", "password": "' + b"x" * (9 * 1024 * 1024) + b'"}'
        r = self.client.post(
            "/users/login", data=body, content_type="application/json"
        )
        self.assertEqual(r.status_code, 413)
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_non_string_json_credentials_are_refused_not_crashed(self):
        r = self.client.post(
            "/users/login", json={"username": 5, "password": ["x"]}
        )
        self.assertEqual(r.status_code, 401)
        self.assertEqual(
            self.client.post(
                "/users/create", json={"username": 5, "password": 7}
            ).status_code,
            401,
        )

    def test_failure_table_drops_expired_keys_and_stays_bounded(self):
        now = [1_000_000.0]
        with patch.object(ks.time, "time", side_effect=lambda: now[0]):
            for i in range(30):
                ks._auth_is_limited((f"rotating-{i}", "192.0.2.1"), failed=True)
            self.assertEqual(len(ks._AUTH_FAILURES), 30)

            now[0] += ks._AUTH_FAILURE_WINDOW + 1
            ks._auth_is_limited(("fresh", "192.0.2.1"), failed=True)
            self.assertEqual(list(ks._AUTH_FAILURES), [("fresh", "192.0.2.1")])

            with patch.object(ks, "_AUTH_FAILURE_MAX_KEYS", 10):
                for i in range(25):
                    ks._auth_is_limited((f"flood-{i}", "192.0.2.1"), failed=True)
                self.assertEqual(len(ks._AUTH_FAILURES), 10)
                self.assertIn(("flood-24", "192.0.2.1"), ks._AUTH_FAILURES)

    def test_failure_key_caps_the_username(self):
        with Flask(__name__).test_request_context("/"):
            username, _address = ks._auth_failure_key("a" * 100_000)
        self.assertEqual(len(username), ks._AUTH_USERNAME_KEY_CHARS)

    def test_key_comparison_is_constant_time(self):
        with patch.object(ks.hmac, "compare_digest", wraps=hmac.compare_digest) as cd:
            self.assertEqual(ks.authenticate_kosync("reader", "right-key")[0], True)
            cd.assert_called()
        self.assertTrue(ks._kosync_creds_match("pässwörd", "pässwörd"))
        self.assertFalse(ks._kosync_creds_match("pässwörd", "other"))
        self.assertFalse(ks._kosync_creds_match(None, "other"))
        self.assertTrue(
            ks._kosync_creds_match(hash_kosync_key("right-key"), "right-key")
        )


if __name__ == "__main__":
    unittest.main()