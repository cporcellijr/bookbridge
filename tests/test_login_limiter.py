"""Dashboard login limiter: per-client keying behind a proxy, and a bounded failure dict."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from src.db.database_service import DatabaseService
from tests.test_webserver import MockContainer

_TEMPLATES = str(Path(__file__).parent.parent / "templates")


class TestLoginLimiter(unittest.TestCase):
    def setUp(self):
        import src.web_server as ws
        self.ws = ws
        self.tmp = tempfile.mkdtemp()
        self._env_keys = ('DATA_DIR', 'BOOKS_DIR', 'TEMPLATE_DIR', 'REMOTE_AUTH_TRUSTED_PROXIES')
        self._saved_env = {k: os.environ.get(k) for k in self._env_keys}
        os.environ.update(DATA_DIR=self.tmp, BOOKS_DIR=self.tmp, TEMPLATE_DIR=_TEMPLATES)
        os.environ.pop('REMOTE_AUTH_TRUSTED_PROXIES', None)
        self._saved_failures = dict(ws._LOGIN_FAILURES)
        self._saved_max = ws._LOGIN_FAILURE_MAX_KEYS
        ws._LOGIN_FAILURES.clear()

        self.svc = DatabaseService(os.path.join(self.tmp, "limiter.db"))
        self.svc.create_user("admin", "secret", role="admin")
        container = MockContainer()
        container.mock_database_service = self.svc
        import src.db.migration_utils
        self._orig_init = src.db.migration_utils.initialize_database
        src.db.migration_utils.initialize_database = lambda data_dir: self.svc
        self.app, _ = ws.create_app(test_container=container)
        self.app.config['TESTING'] = True
        self.app.config['LOGIN_DISABLED'] = False

    def tearDown(self):
        import src.db.migration_utils
        src.db.migration_utils.initialize_database = self._orig_init
        self.ws._LOGIN_FAILURES.clear()
        self.ws._LOGIN_FAILURES.update(self._saved_failures)
        self.ws._LOGIN_FAILURE_MAX_KEYS = self._saved_max
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _post(self, username="admin", password="wrong", peer="127.0.0.1", xff=None):
        headers = {'X-Forwarded-For': xff} if xff else {}
        return self.app.test_client().post(
            '/login',
            data={'username': username, 'password': password},
            environ_base={'REMOTE_ADDR': peer},
            headers=headers,
        )

    def test_five_failures_then_429(self):
        for _ in range(5):
            self.assertEqual(self._post().status_code, 401)
        resp = self._post()
        self.assertEqual(resp.status_code, 429)
        self.assertIn(b"Too many login attempts. Try again shortly.", resp.data)

    def test_trusted_proxy_limits_each_forwarded_client_independently(self):
        for _ in range(5):
            self._post(xff="203.0.113.1")
        self.assertEqual(self._post(xff="203.0.113.1").status_code, 429)
        self.assertEqual(self._post(xff="203.0.113.2").status_code, 401)

    def test_untrusted_peer_cannot_spoof_forwarded_for(self):
        for i in range(5):
            self._post(peer="198.51.100.9", xff=f"203.0.113.{i}")
        self.assertEqual(self._post(peer="198.51.100.9", xff="203.0.113.99").status_code, 429)

    def test_failure_dict_is_capped(self):
        self.ws._LOGIN_FAILURE_MAX_KEYS = 5
        for i in range(20):
            self._post(username=f"user{i}")
        self.assertLessEqual(len(self.ws._LOGIN_FAILURES), 5)
        self.assertIn(("user19", "127.0.0.1"), self.ws._LOGIN_FAILURES)


if __name__ == '__main__':
    unittest.main()
