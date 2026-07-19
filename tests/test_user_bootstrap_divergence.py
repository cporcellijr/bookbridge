"""Tests for credential divergence warning in user bootstrap (GitHub #328)."""

import shutil
import tempfile
import unittest

from src.db.user_bootstrap import _warn_on_credential_divergence, bootstrap_admin_user
from src.db.database_service import DatabaseService


class TestUserBootstrapDivergence(unittest.TestCase):
    """Test boot-time warning when admin per-user credentials diverge from global settings."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db = DatabaseService(db_path=f"{self.tmpdir}/test.db")

    def tearDown(self):
        self.db.db_manager.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _create_admin(self):
        return self.db.create_user("admin", "password", role="admin")

    def test_warns_on_divergent_abs_key(self):
        """Divergent ABS_KEY between global settings and admin account warns."""
        admin = self._create_admin()
        self.db.set_setting("ABS_KEY", "old-global-token")
        self.db.set_user_credential(admin.id, "ABS_KEY", "new-account-token")

        with self.assertLogs("src.db.user_bootstrap", level="WARNING") as cm:
            result = _warn_on_credential_divergence(self.db, admin)

        self.assertIn("ABS_KEY", result)
        self.assertTrue(any("Credential divergence: global ABS_KEY" in m for m in cm.output))

    def test_no_warning_when_consistent(self):
        """Same value in both stores produces no warning."""
        admin = self._create_admin()
        self.db.set_setting("ABS_KEY", "same-token")
        self.db.set_user_credential(admin.id, "ABS_KEY", "same-token")

        result = _warn_on_credential_divergence(self.db, admin)
        self.assertEqual(result, [])

    def test_no_warning_when_per_user_blank_falls_back(self):
        """Blank per-user + set global is healthy (admin falls back to global)."""
        admin = self._create_admin()
        self.db.set_setting("ABS_KEY", "global-token")
        # No per-user credential set

        result = _warn_on_credential_divergence(self.db, admin)
        self.assertEqual(result, [])

    def test_warns_when_global_blank_but_account_set(self):
        """Per-user set + blank global warns (background uses empty global)."""
        admin = self._create_admin()
        # No global setting
        self.db.set_user_credential(admin.id, "ABS_KEY", "account-token")

        with self.assertLogs("src.db.user_bootstrap", level="WARNING") as cm:
            result = _warn_on_credential_divergence(self.db, admin)

        self.assertIn("ABS_KEY", result)
        self.assertTrue(any("Credential divergence: global ABS_KEY" in m for m in cm.output))

    def test_bootstrap_admin_user_emits_warning_end_to_end(self):
        """End-to-end bootstrap emits warning for pre-seeded divergence."""
        admin = self._create_admin()
        # Pre-seed divergence: global set, then admin saves different per-user value
        self.db.set_setting("ABS_KEY", "old-global-token")
        self.db.set_user_credential(admin.id, "ABS_KEY", "new-account-token")

        with self.assertLogs("src.db.user_bootstrap", level="WARNING") as cm:
            bootstrap_admin_user(self.db)

        # Prefill must not overwrite the existing non-empty per-user row
        self.assertEqual(self.db.get_user_credential(admin.id, "ABS_KEY"), "new-account-token")
        self.assertTrue(any("Credential divergence: global ABS_KEY" in m for m in cm.output))


if __name__ == "__main__":
    unittest.main()