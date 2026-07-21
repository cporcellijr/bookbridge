"""Regression tests for issue #336 — credentials stored in plaintext at rest.

The bar these enforce: after a credential is written through the normal
application path, the value sitting in the SQLite row must not be the plaintext
the user typed, while every existing reader still gets the plaintext back.
"""

import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.db.database_service import DatabaseService
from src.utils import secret_store


class SecretStoreTestCase(unittest.TestCase):
    """Common temp DATA_DIR + key-cache hygiene.

    ``secret_store`` caches its Fernet instance in a module global; leaving a
    key from one test in place would poison the rest of the suite, which must
    pass in any order.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._saved_env = {
            k: os.environ.get(k)
            for k in ("DATA_DIR", "BOOKBRIDGE_SECRET_KEY", "BOOKBRIDGE_SECRET_KEY_FILE")
        }
        os.environ["DATA_DIR"] = self.tmpdir
        os.environ.pop("BOOKBRIDGE_SECRET_KEY", None)
        os.environ.pop("BOOKBRIDGE_SECRET_KEY_FILE", None)
        secret_store.reset_cache()

    def tearDown(self):
        secret_store.reset_cache()
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        secret_store.reset_cache()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _service(self) -> DatabaseService:
        return DatabaseService(str(Path(self.tmpdir) / "database.db"))

    def _raw_rows(self, table: str, key: str) -> list:
        """Read the value straight out of SQLite, bypassing DatabaseService."""
        conn = sqlite3.connect(str(Path(self.tmpdir) / "database.db"))
        try:
            cur = conn.execute(f"SELECT value FROM {table} WHERE key = ?", (key,))
            return [r[0] for r in cur.fetchall()]
        finally:
            conn.close()


class TestSecretStorePrimitives(SecretStoreTestCase):
    def test_key_file_is_generated_in_data_dir(self):
        self.assertTrue(secret_store.available())
        self.assertTrue((Path(self.tmpdir) / "secret.key").exists())

    def test_roundtrip(self):
        token = secret_store.encrypt("hunter2")
        self.assertTrue(secret_store.is_encrypted(token))
        self.assertNotIn("hunter2", token)
        self.assertEqual(secret_store.decrypt(token), "hunter2")

    def test_encryption_is_non_deterministic(self):
        """Two rows with the same password must not produce the same ciphertext."""
        self.assertNotEqual(secret_store.encrypt("same"), secret_store.encrypt("same"))

    def test_empty_and_none_pass_through(self):
        self.assertIsNone(secret_store.encrypt(None))
        self.assertEqual(secret_store.encrypt(""), "")

    def test_double_encrypt_is_a_no_op(self):
        once = secret_store.encrypt("hunter2")
        self.assertEqual(secret_store.encrypt(once), once)

    def test_legacy_plaintext_passes_through_decrypt(self):
        self.assertEqual(secret_store.decrypt("hunter2"), "hunter2")

    def test_env_key_overrides_key_file(self):
        os.environ["BOOKBRIDGE_SECRET_KEY"] = "a-long-operator-chosen-passphrase"
        secret_store.reset_cache()
        token = secret_store.encrypt("hunter2")
        self.assertFalse((Path(self.tmpdir) / "secret.key").exists())
        self.assertEqual(secret_store.decrypt(token), "hunter2")

    def test_wrong_key_yields_empty_not_ciphertext(self):
        """A restored DB without its key must read as 'not configured' — never
        hand the raw token to a sync client to replay as a password."""
        token = secret_store.encrypt("hunter2")
        os.environ["BOOKBRIDGE_SECRET_KEY"] = "a-completely-different-passphrase"
        secret_store.reset_cache()
        self.assertEqual(secret_store.decrypt(token), "")

    def test_secret_keys_cover_the_reported_credentials(self):
        keys = secret_store.secret_keys()
        for key in (
            "KOSYNC_KEY", "STORYTELLER_PASSWORD", "CWA_PASSWORD",
            "BOOKORBIT_PASSWORD", "BOOKLORE_PASSWORD", "BOOKORBIT_KOSYNC_KEY",
            "ABS_KEY", "HARDCOVER_TOKEN", "STORYGRAPH_SESSION_COOKIE",
            "READEST_PASSWORD", "BOOKFUSION_API_KEY", "CWA_SYNC_TOKEN",
            "LLM_API_KEY", "DEEPGRAM_API_KEY", "TELEGRAM_BOT_TOKEN",
            "WEB_SECRET_KEY",
        ):
            self.assertIn(key, keys, f"{key} must be encrypted at rest")

    def test_every_per_user_secret_field_is_covered(self):
        """New 'secret' fields on the per-user page are encrypted automatically."""
        from src.utils.user_config import PER_USER_FIELD_GROUPS

        declared = {
            key
            for _g, fields in PER_USER_FIELD_GROUPS
            for key, _l, ftype in fields
            if ftype == "secret"
        }
        self.assertTrue(declared)
        self.assertTrue(declared.issubset(secret_store.secret_keys()))


class TestUserCredentialsAtRest(SecretStoreTestCase):
    def test_kosync_key_is_not_plaintext_in_the_database(self):
        """The exact reproduction from issue #336, step 3."""
        svc = self._service()
        user = svc.create_user("reader", "pw-for-login")
        svc.set_user_credential(user.id, "KOSYNC_KEY", "s3cret-sync-pass")

        stored = self._raw_rows("user_credentials", "KOSYNC_KEY")
        self.assertEqual(len(stored), 1)
        self.assertNotEqual(stored[0], "s3cret-sync-pass")
        self.assertNotIn("s3cret-sync-pass", stored[0])
        self.assertTrue(secret_store.is_encrypted(stored[0]))

        self.assertEqual(
            svc.get_user_credential(user.id, "KOSYNC_KEY"), "s3cret-sync-pass"
        )
        self.assertEqual(
            svc.get_user_credentials(user.id)["KOSYNC_KEY"], "s3cret-sync-pass"
        )

    def test_every_reported_credential_key_is_encrypted(self):
        svc = self._service()
        user = svc.create_user("reader", "pw-for-login")
        samples = {
            "STORYTELLER_PASSWORD": "st-pass",
            "CWA_PASSWORD": "cwa-pass",
            "BOOKORBIT_PASSWORD": "bo-pass",
            "BOOKLORE_PASSWORD": "bl-pass",
            "BOOKORBIT_KOSYNC_KEY": "bo-kosync",
            "ABS_KEY": "abs-token",
            "HARDCOVER_TOKEN": "hc-token",
        }
        for key, value in samples.items():
            svc.set_user_credential(user.id, key, value)

        creds = svc.get_user_credentials(user.id)
        for key, value in samples.items():
            raw = self._raw_rows("user_credentials", key)[0]
            self.assertNotIn(value, raw, f"{key} leaked plaintext into the DB")
            self.assertEqual(creds[key], value, f"{key} did not round-trip")

    def test_non_secret_credentials_stay_readable(self):
        """Usernames, library ids and toggles are not secrets — leave them plain
        so operators can still inspect and support an install."""
        svc = self._service()
        user = svc.create_user("reader", "pw-for-login")
        svc.set_user_credential(user.id, "KOSYNC_USER", "reader-device")
        self.assertEqual(self._raw_rows("user_credentials", "KOSYNC_USER")[0], "reader-device")

    def test_update_of_existing_credential_re_encrypts(self):
        svc = self._service()
        user = svc.create_user("reader", "pw-for-login")
        svc.set_user_credential(user.id, "KOSYNC_KEY", "first")
        svc.set_user_credential(user.id, "KOSYNC_KEY", "second")
        raw = self._raw_rows("user_credentials", "KOSYNC_KEY")[0]
        self.assertNotIn("second", raw)
        self.assertEqual(svc.get_user_credential(user.id, "KOSYNC_KEY"), "second")

    def test_setter_returns_plaintext_to_its_caller(self):
        """Existing callers read `.value` off the returned row."""
        svc = self._service()
        user = svc.create_user("reader", "pw-for-login")
        created = svc.set_user_credential(user.id, "KOSYNC_KEY", "first")
        self.assertEqual(created.value, "first")
        updated = svc.set_user_credential(user.id, "KOSYNC_KEY", "second")
        self.assertEqual(updated.value, "second")


class TestGlobalSettingsAtRest(SecretStoreTestCase):
    def test_admin_secrets_are_encrypted(self):
        svc = self._service()
        svc.set_setting("ABS_KEY", "admin-abs-token")
        raw = self._raw_rows("settings", "ABS_KEY")[0]
        self.assertNotIn("admin-abs-token", raw)
        self.assertEqual(svc.get_setting("ABS_KEY"), "admin-abs-token")
        self.assertEqual(svc.get_all_settings()["ABS_KEY"], "admin-abs-token")

    def test_non_secret_settings_are_untouched(self):
        svc = self._service()
        svc.set_setting("ABS_SERVER", "http://abs.local")
        self.assertEqual(self._raw_rows("settings", "ABS_SERVER")[0], "http://abs.local")

    def test_setter_returns_plaintext_to_its_caller(self):
        svc = self._service()
        self.assertEqual(svc.set_setting("ABS_KEY", "one").value, "one")
        self.assertEqual(svc.set_setting("ABS_KEY", "two").value, "two")

    def test_empty_secret_stays_empty(self):
        """Bootstrap seeds unset keys with '' — that must not become ciphertext,
        or `if val:` guards across the app would start seeing a truthy value."""
        svc = self._service()
        svc.set_setting("ABS_KEY", "")
        self.assertEqual(self._raw_rows("settings", "ABS_KEY")[0], "")
        self.assertEqual(svc.get_setting("ABS_KEY"), "")


class TestPlaintextMigration(SecretStoreTestCase):
    def _write_legacy_plaintext(self, svc: DatabaseService, user_id: int) -> None:
        """Simulate an install that predates encryption by writing raw rows."""
        conn = sqlite3.connect(str(Path(self.tmpdir) / "database.db"))
        try:
            conn.execute(
                "UPDATE user_credentials SET value = ? WHERE user_id = ? AND key = ?",
                ("legacy-sync-pass", user_id, "KOSYNC_KEY"),
            )
            conn.execute(
                "UPDATE settings SET value = ? WHERE key = ?",
                ("legacy-abs-token", "ABS_KEY"),
            )
            conn.commit()
        finally:
            conn.close()

    def test_existing_plaintext_is_readable_before_migration(self):
        """Upgrades must not lock anyone out while the sweep has not run."""
        svc = self._service()
        user = svc.create_user("reader", "pw-for-login")
        svc.set_user_credential(user.id, "KOSYNC_KEY", "placeholder")
        svc.set_setting("ABS_KEY", "placeholder")
        self._write_legacy_plaintext(svc, user.id)

        self.assertEqual(svc.get_user_credential(user.id, "KOSYNC_KEY"), "legacy-sync-pass")
        self.assertEqual(svc.get_setting("ABS_KEY"), "legacy-abs-token")

    def test_sweep_encrypts_both_stores_and_preserves_values(self):
        svc = self._service()
        user = svc.create_user("reader", "pw-for-login")
        svc.set_user_credential(user.id, "KOSYNC_KEY", "placeholder")
        svc.set_setting("ABS_KEY", "placeholder")
        self._write_legacy_plaintext(svc, user.id)

        self.assertEqual(svc.encrypt_plaintext_secrets(), 2)

        self.assertNotIn("legacy-sync-pass", self._raw_rows("user_credentials", "KOSYNC_KEY")[0])
        self.assertNotIn("legacy-abs-token", self._raw_rows("settings", "ABS_KEY")[0])
        self.assertEqual(svc.get_user_credential(user.id, "KOSYNC_KEY"), "legacy-sync-pass")
        self.assertEqual(svc.get_setting("ABS_KEY"), "legacy-abs-token")

    def test_sweep_is_idempotent(self):
        svc = self._service()
        user = svc.create_user("reader", "pw-for-login")
        svc.set_user_credential(user.id, "KOSYNC_KEY", "placeholder")
        self._write_legacy_plaintext(svc, user.id)

        self.assertEqual(svc.encrypt_plaintext_secrets(), 1)
        self.assertEqual(svc.encrypt_plaintext_secrets(), 0)

    def test_sweep_leaves_non_secret_rows_alone(self):
        svc = self._service()
        svc.set_setting("ABS_SERVER", "http://abs.local")
        svc.encrypt_plaintext_secrets()
        self.assertEqual(self._raw_rows("settings", "ABS_SERVER")[0], "http://abs.local")


class TestKosyncAuthAgainstEncryptedStorage(SecretStoreTestCase):
    """The KoSync server authenticates devices against the stored value. Issue
    #336 suggested storing only MD5 for KOSYNC_KEY; that would break the CWA
    HTTP Basic auth method, which needs the raw password. These tests pin both
    the device-auth path and the outbound raw-key requirement."""

    def test_device_authenticates_with_raw_key_and_md5(self):
        from src.api import kosync_server

        svc = self._service()
        user = svc.create_user("reader", "pw-for-login")
        svc.set_user_credential(user.id, "KOSYNC_USER", "reader-device")
        svc.set_user_credential(user.id, "KOSYNC_KEY", "s3cret-sync-pass")

        saved = kosync_server._database_service
        try:
            kosync_server._database_service = svc
            md5 = kosync_server.hash_kosync_key("s3cret-sync-pass")

            ok, uid = kosync_server.authenticate_kosync("reader-device", "s3cret-sync-pass")
            self.assertTrue(ok)
            self.assertEqual(uid, user.id)

            ok, uid = kosync_server.authenticate_kosync("reader-device", md5)
            self.assertTrue(ok)
            self.assertEqual(uid, user.id)

            ok, _ = kosync_server.authenticate_kosync("reader-device", "wrong-pass")
            self.assertFalse(ok)
        finally:
            kosync_server._database_service = saved

    def test_ciphertext_is_not_accepted_as_a_password(self):
        """A device presenting the stored ciphertext must not authenticate."""
        from src.api import kosync_server

        svc = self._service()
        user = svc.create_user("reader", "pw-for-login")
        svc.set_user_credential(user.id, "KOSYNC_USER", "reader-device")
        svc.set_user_credential(user.id, "KOSYNC_KEY", "s3cret-sync-pass")
        ciphertext = self._raw_rows("user_credentials", "KOSYNC_KEY")[0]

        saved = kosync_server._database_service
        try:
            kosync_server._database_service = svc
            ok, _ = kosync_server.authenticate_kosync("reader-device", ciphertext)
            self.assertFalse(ok)
        finally:
            kosync_server._database_service = saved

    def test_basic_auth_still_receives_the_raw_password(self):
        """Why KOSYNC_KEY cannot be reduced to an MD5 hash at rest."""
        from src.utils.kosync_headers import kosync_request_kwargs

        kwargs = kosync_request_kwargs("reader-device", "s3cret-sync-pass", "basic")
        self.assertEqual(kwargs["auth"], ("reader-device", "s3cret-sync-pass"))


if __name__ == "__main__":
    unittest.main()
