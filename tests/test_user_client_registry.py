import os
import tempfile
import shutil
import unittest
from unittest.mock import Mock

from src.db.database_service import DatabaseService
from src.services.user_client_registry import UserClientRegistry, UserClients


class TestUserClientRegistry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.svc = DatabaseService(os.path.join(self.tmp, "mu.db"))
        # Global ABS server (shared) + a global token (admin)
        os.environ['ABS_SERVER'] = 'https://abs.example'
        os.environ['ABS_KEY'] = 'global-token'
        self.registry = UserClientRegistry(
            database_service=self.svc,
            ebook_parser=Mock(),
            alignment_service=Mock(),
            transcriber=Mock(),
            ollama_client=None,
        )

    def tearDown(self):
        os.environ.pop('ABS_KEY', None)
        os.environ.pop('ABS_SERVER', None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_bundle_uses_user_credentials(self):
        u = self.svc.create_user("alice", "pw")
        self.svc.set_user_credential(u.id, "ABS_KEY", "alice-token")
        bundle = self.registry.get_clients(u.id)
        self.assertIsInstance(bundle, UserClients)
        # per-user token overrides global; shared server URL falls through
        self.assertEqual(bundle.abs_client.token, "alice-token")
        self.assertEqual(bundle.abs_client.base_url, "https://abs.example")

    def test_abs_token_strips_pasted_bearer_prefix(self):
        u = self.svc.create_user("bearer", "pw")
        self.svc.set_user_credential(u.id, "ABS_KEY", "Bearer pasted-token")
        bundle = self.registry.get_clients(u.id)
        self.assertEqual(bundle.abs_client.token, "pasted-token")
        self.assertEqual(bundle.abs_client.headers["Authorization"], "Bearer pasted-token")

    def test_regular_bundle_does_not_fall_back_to_global_account_token(self):
        u = self.svc.create_user("bob", "pw")  # no per-user ABS_KEY
        bundle = self.registry.get_clients(u.id)
        self.assertEqual(bundle.abs_client.token, "")

    def test_regular_bundle_does_not_inherit_global_provider_accounts(self):
        env = {
            "BOOKLORE_SERVER": "https://grimmory.example",
            "BOOKLORE_ENABLED": "true",
            "BOOKLORE_USER": "global-grimmory",
            "BOOKLORE_PASSWORD": "global-password",
            "CWA_SERVER": "https://cwa.example",
            "CWA_ENABLED": "true",
            "CWA_USERNAME": "global-cwa",
            "CWA_PASSWORD": "global-password",
            "HARDCOVER_ENABLED": "true",
            "HARDCOVER_TOKEN": "global-hardcover",
        }
        old = {key: os.environ.get(key) for key in env}
        try:
            os.environ.update(env)
            u = self.svc.create_user("no-global-providers", "pw")
            bundle = self.registry.get_clients(u.id)

            self.assertFalse(bundle.booklore_client.is_configured())
            self.assertFalse(bundle.cwa_client.is_configured())
            self.assertFalse(bundle.hardcover_client.is_configured())
        finally:
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_admin_bundle_falls_back_to_global_when_unset(self):
        u = self.svc.create_user("admin2", "pw", role="admin")
        bundle = self.registry.get_clients(u.id)
        self.assertEqual(bundle.abs_client.token, "global-token")

    def test_per_user_isolation(self):
        a = self.svc.create_user("ua", "pw")
        b = self.svc.create_user("ub", "pw")
        self.svc.set_user_credential(a.id, "ABS_KEY", "tok-a")
        self.svc.set_user_credential(b.id, "ABS_KEY", "tok-b")
        self.assertEqual(self.registry.get_clients(a.id).abs_client.token, "tok-a")
        self.assertEqual(self.registry.get_clients(b.id).abs_client.token, "tok-b")

    def test_sync_clients_present(self):
        u = self.svc.create_user("carol", "pw")
        bundle = self.registry.get_clients(u.id)
        for key in ("ABS", "KoSync", "Storyteller", "BookLore", "BookOrbit", "CWA", "Hardcover", "StoryGraph"):
            self.assertIn(key, bundle.sync_clients)

    def test_cache_and_invalidate(self):
        u = self.svc.create_user("dave", "pw")
        b1 = self.registry.get_clients(u.id)
        b2 = self.registry.get_clients(u.id)
        self.assertIs(b1, b2)  # cached
        self.registry.invalidate(u.id)
        b3 = self.registry.get_clients(u.id)
        self.assertIsNot(b1, b3)  # rebuilt after invalidate

    def test_invalidate_picks_up_credential_change(self):
        u = self.svc.create_user("erin", "pw")
        self.svc.set_user_credential(u.id, "ABS_KEY", "old")
        self.assertEqual(self.registry.get_clients(u.id).abs_client.token, "old")
        self.svc.set_user_credential(u.id, "ABS_KEY", "new")
        self.registry.invalidate(u.id)
        self.assertEqual(self.registry.get_clients(u.id).abs_client.token, "new")

    def test_transient_db_exception_does_not_cache_fallback(self):
        """A DB exception on the first credential read returns a fallback bundle
        for that request but does NOT place it in the cache. A subsequent
        successful read must return real credentials and be cached."""
        u = self.svc.create_user("frank", "pw")
        self.svc.set_user_credential(u.id, "ABS_KEY", "real-token")

        call_count = [0]
        original_get_creds = self.svc.get_user_credentials

        def _flaky_get_creds(uid):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("simulated DB failure")
            return original_get_creds(uid)

        self.svc.get_user_credentials = _flaky_get_creds
        try:
            # First call: DB exception → fallback returned, NOT cached
            fallback = self.registry.get_clients(u.id)
            self.assertIsInstance(fallback, UserClients)
            self.assertEqual(fallback.abs_client.token, "")
            self.assertNotIn(u.id, self.registry._cache)

            # Second call: DB succeeds → real credentials, now cached
            real = self.registry.get_clients(u.id)
            self.assertEqual(real.abs_client.token, "real-token")
            self.assertIn(u.id, self.registry._cache)
            self.assertIs(real, self.registry._cache[u.id])

            # Third call: served from cache
            cached = self.registry.get_clients(u.id)
            self.assertIs(cached, real)
        finally:
            self.svc.get_user_credentials = original_get_creds


if __name__ == "__main__":
    unittest.main()
