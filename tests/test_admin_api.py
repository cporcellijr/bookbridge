"""Token-authenticated admin API for provisioning readers."""
import hashlib
import logging
import os
import shutil
import tempfile
import unittest
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

from flask import Flask

from src.api import admin_api
from src.api import kosync_server
from src.db.database_service import DatabaseService
from src.db.models import User, UserCredential

TOKEN = 't' * 40


class TestAdminApi(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # A private app and database, so nothing leaks into other test modules.
        cls.tmpdir = tempfile.mkdtemp(prefix="test_admin_api_")
        cls.db = DatabaseService(os.path.join(cls.tmpdir, "test.db"))
        cls.app = Flask(__name__)
        cls.app.register_blueprint(admin_api.admin_api_bp)
        cls.app.register_blueprint(kosync_server.kosync_sync_bp)
        cls.client = cls.app.test_client()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def setUp(self):
        admin_api.init_admin_api(self.db, MagicMock(return_value=0), MagicMock())
        self.addCleanup(admin_api.init_admin_api, None)
        with self.db.get_session() as session:
            session.query(UserCredential).delete()
            session.query(User).delete()
        self.db.create_user("admin", "admin-password", role="admin")
        admin_api._AUTH_FAILURES.clear()
        env = patch.dict(os.environ, {"ADMIN_API_TOKEN": TOKEN})
        env.start()
        self.addCleanup(env.stop)

    def _h(self, token=TOKEN):
        return {"Authorization": f"Bearer {token}"}

    # --- access -----------------------------------------------------------------

    def test_disabled_without_token(self):
        with patch.dict(os.environ, {"ADMIN_API_TOKEN": ""}):
            r = self.client.get('/api/v1/admin/users', headers=self._h())
        self.assertEqual(r.status_code, 404)

    def test_disabled_with_short_token(self):
        with patch.dict(os.environ, {"ADMIN_API_TOKEN": "short"}):
            r = self.client.get('/api/v1/admin/users', headers=self._h("short"))
        self.assertEqual(r.status_code, 404)

    def test_wrong_or_missing_token_is_401(self):
        self.assertEqual(self.client.get('/api/v1/admin/users', headers=self._h('x' * 40)).status_code, 401)
        self.assertEqual(self.client.get('/api/v1/admin/users').status_code, 401)

    def test_list_users_never_includes_secrets(self):
        r = self.client.get('/api/v1/admin/users', headers=self._h())
        self.assertEqual(r.status_code, 200)
        users = r.get_json()["users"]
        self.assertEqual([u["username"] for u in users], ["admin"])
        self.assertEqual(set(users[0]), {"id", "username", "role", "active"})

    # --- users ------------------------------------------------------------------

    def test_create_user_then_idempotent_update(self):
        r = self.client.put('/api/v1/admin/users/reader@example.com', headers=self._h(),
                            json={"password": "first password here"})
        self.assertEqual(r.status_code, 201)
        body = r.get_json()
        self.assertEqual((body["username"], body["role"], body["active"], body["created"]),
                         ("reader@example.com", "user", True, True))
        admin_api._on_user_created.assert_called_once()
        self.assertIsNotNone(self.db.verify_user_credentials("reader@example.com", "first password here"))

        r = self.client.put('/api/v1/admin/users/reader@example.com', headers=self._h(),
                            json={"password": "second password here"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.get_json()["created"])
        self.assertIsNone(self.db.verify_user_credentials("reader@example.com", "first password here"))
        self.assertIsNotNone(self.db.verify_user_credentials("reader@example.com", "second password here"))
        admin_api._on_user_created.assert_called_once()

    def test_create_requires_password(self):
        r = self.client.put('/api/v1/admin/users/new@example.com', headers=self._h(), json={})
        self.assertEqual(r.status_code, 400)
        self.assertIsNone(self.db.get_user_by_username("new@example.com"))

    def test_explicit_active_flag(self):
        self.client.put('/api/v1/admin/users/r@example.com', headers=self._h(), json={"password": "pw pw pw"})
        r = self.client.put('/api/v1/admin/users/r@example.com', headers=self._h(), json={"active": False})
        self.assertFalse(r.get_json()["active"])
        # Same value again is a no-op, not a toggle.
        r = self.client.put('/api/v1/admin/users/r@example.com', headers=self._h(), json={"active": False})
        self.assertFalse(r.get_json()["active"])
        r = self.client.put('/api/v1/admin/users/r@example.com', headers=self._h(), json={"active": True})
        self.assertTrue(r.get_json()["active"])

    def test_admin_accounts_cannot_be_modified_through_the_api(self):
        before = self.db.get_user_by_username("admin").password_hash
        for body in ({"active": False}, {"role": "user"}, {"password": "takeover password"}, {"role": "admin"}):
            r = self.client.put('/api/v1/admin/users/admin', headers=self._h(), json=body)
            self.assertEqual(r.status_code, 403, body)
            self.assertIn("reader accounts only", r.get_json()["error"])
        admin = self.db.get_user_by_username("admin")
        self.assertTrue(admin.active)
        self.assertEqual(admin.password_hash, before)

    def test_admins_cannot_be_created_or_promoted_through_the_api(self):
        r = self.client.put('/api/v1/admin/users/new-admin', headers=self._h(),
                            json={"password": "pw pw pw", "role": "admin"})
        self.assertEqual(r.status_code, 403)
        self.assertIsNone(self.db.get_user_by_username("new-admin"))
        self.client.put('/api/v1/admin/users/reader', headers=self._h(), json={"password": "pw pw pw"})
        r = self.client.put('/api/v1/admin/users/reader', headers=self._h(), json={"role": "admin"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.db.get_user_by_username("reader").role, "user")

    def test_admin_integrations_cannot_be_patched_through_the_api(self):
        r = self.client.patch('/api/v1/admin/users/admin/integrations', headers=self._h(),
                              json={"KOSYNC_USER": "hijack"})
        self.assertEqual(r.status_code, 403)
        admin = self.db.get_user_by_username("admin")
        self.assertIsNone(self.db.get_user_credential(admin.id, "KOSYNC_USER"))
        admin_api._on_integrations_saved.assert_not_called()

    def test_last_active_admin_policy_lives_in_the_database(self):
        admin = self.db.get_user_by_username("admin")
        for kwargs in ({"active": False}, {"role": "user"}):
            with self.assertRaises(ValueError):
                self.db.update_user_account(admin.id, **kwargs)
        self.assertTrue(self.db.get_user_by_username("admin").active)

    def test_bad_inputs(self):
        for body in ({"password": ""}, {"role": "owner"}, {"active": "maybe"}):
            r = self.client.put('/api/v1/admin/users/x@example.com', headers=self._h(), json=body)
            self.assertEqual(r.status_code, 400, body)
        r = self.client.put('/api/v1/admin/users/x@example.com', headers=self._h(), data="not json")
        self.assertEqual(r.status_code, 400)

    # --- integrations ---------------------------------------------------------

    def test_patch_integrations_sets_only_given_keys_and_hides_secrets(self):
        self.client.put('/api/v1/admin/users/r@example.com', headers=self._h(), json={"password": "pw pw pw"})
        uid = self.db.get_user_by_username("r@example.com").id
        self.db.set_user_credential(uid, "ABS_LIBRARY_ID", "lib-1")
        r = self.client.patch('/api/v1/admin/users/r@example.com/integrations', headers=self._h(), json={
            "KOSYNC_ENABLED": True, "KOSYNC_USER": "r@example.com", "KOSYNC_KEY": "sync secret",
            "READEST_ENABLED": False,
        })
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["updated"], ["KOSYNC_ENABLED", "KOSYNC_KEY", "KOSYNC_USER", "READEST_ENABLED"])
        self.assertNotIn("sync secret", r.get_data(as_text=True))
        creds = self.db.get_user_credentials(uid)
        self.assertEqual(creds["KOSYNC_ENABLED"], "true")
        self.assertEqual(creds["READEST_ENABLED"], "false")
        self.assertEqual(creds["KOSYNC_KEY"], "sync secret")
        self.assertEqual(creds["ABS_LIBRARY_ID"], "lib-1")  # untouched
        admin_api._on_integrations_saved.assert_called_once_with(uid)

    def test_patch_integrations_rejects_unknown_keys_and_bad_bools(self):
        self.client.put('/api/v1/admin/users/r@example.com', headers=self._h(), json={"password": "pw pw pw"})
        r = self.client.patch('/api/v1/admin/users/r@example.com/integrations', headers=self._h(),
                              json={"NOT_A_KEY": "x"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["keys"], ["NOT_A_KEY"])
        r = self.client.patch('/api/v1/admin/users/r@example.com/integrations', headers=self._h(),
                              json={"KOSYNC_ENABLED": "sometimes"})
        self.assertEqual(r.status_code, 400)
        r = self.client.patch('/api/v1/admin/users/nobody@example.com/integrations', headers=self._h(),
                              json={"KOSYNC_ENABLED": True})
        self.assertEqual(r.status_code, 404)

    def test_patch_integrations_validates_values_by_declared_type(self):
        self.client.put('/api/v1/admin/users/r@example.com', headers=self._h(), json={"password": "pw pw pw"})
        uid = self.db.get_user_by_username("r@example.com").id
        url = '/api/v1/admin/users/r@example.com/integrations'
        select_key = next(k for k, t in admin_api._field_types().items() if t.startswith("select:"))
        for body in (
            {select_key: "not-a-declared-option"},
            {select_key: True},
            {"KOSYNC_USER": True},
            {"KOSYNC_USER": 12},
            {"KOSYNC_USER": None},
            {"KOSYNC_ENABLED": "true"},
            {"KOSYNC_ENABLED": 1},
        ):
            r = self.client.patch(url, headers=self._h(), json=body)
            self.assertEqual(r.status_code, 400, body)
            self.assertIn(next(iter(body)), r.get_json()["error"])
        self.assertEqual(self.db.get_user_credentials(uid), {})
        option = admin_api._field_types()[select_key][len("select:"):].split("|")[0].split("=")[0]
        r = self.client.patch(url, headers=self._h(), json={select_key: option})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.db.get_user_credential(uid, select_key), option)

    def test_patch_integrations_reports_services_switched_off_server_wide(self):
        """A switch the global gate makes inert is saved, and the response says it is inert."""
        self.client.put('/api/v1/admin/users/r@example.com', headers=self._h(), json={"password": "pw pw pw"})
        uid = self.db.get_user_by_username("r@example.com").id
        url = '/api/v1/admin/users/r@example.com/integrations'
        body = {
            "KOSYNC_ENABLED": True, "KOSYNC_USER": "r@example.com",  # on, gate off: reported
            "ABS_ENABLED": True,                                     # on, gate on: not reported
            "READEST_ENABLED": False,                                # off in this call: not reported
            "READEST_ANNOTATION_SYNC": True,                         # not a service gate: never reported
        }
        globals_off = {"KOSYNC_ENABLED": "false", "ABS_ENABLED": "true", "READEST_ENABLED": "false",
                       "READEST_ANNOTATION_SYNC": "false"}
        with patch.dict(os.environ, globals_off), self.assertLogs(admin_api.logger, level="WARNING") as logs:
            r = self.client.patch(url, headers=self._h(), json=body)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["off_server_wide"], ["KOSYNC_ENABLED"])
        self.assertEqual(r.get_json()["updated"], sorted(body))
        self.assertEqual(self.db.get_user_credential(uid, "KOSYNC_ENABLED"), "true")  # still saved
        self.assertTrue(any("KOSYNC_ENABLED" in rec.getMessage() and "server-wide" in rec.getMessage()
                            for rec in logs.records))

        # A key the call does not touch is not reported, even though it is gated.
        with patch.dict(os.environ, globals_off):
            r = self.client.patch(url, headers=self._h(), json={"KOSYNC_USER": "other"})
        self.assertEqual(r.get_json()["off_server_wide"], [])

        # With the gate open, or no global decision at all, nothing is reported.
        with patch.dict(os.environ, {"KOSYNC_ENABLED": "true"}):
            r = self.client.patch(url, headers=self._h(), json={"KOSYNC_ENABLED": True})
        self.assertEqual(r.get_json()["off_server_wide"], [])
        with patch.dict(os.environ, {"KOSYNC_ENABLED": "x"}):
            del os.environ["KOSYNC_ENABLED"]
            r = self.client.patch(url, headers=self._h(), json={"KOSYNC_ENABLED": True})
        self.assertEqual(r.get_json()["off_server_wide"], [])

    def test_get_user_reports_services_switched_off_server_wide(self):
        self.client.put('/api/v1/admin/users/r@example.com', headers=self._h(), json={"password": "pw pw pw"})
        uid = self.db.get_user_by_username("r@example.com").id
        self.db.set_user_credentials(uid, {
            "KOSYNC_ENABLED": "true", "KOSYNC_KEY": "sync secret",  # on, gate off: reported
            "CWA_SYNC_ENABLED": "true",                             # on, gate off: reported
            "ABS_ENABLED": "true",                                  # on, gate on: not reported
            "READEST_ENABLED": "false",                             # off for this user: not reported
            "READEST_ANNOTATION_SYNC": "true",                      # not a service gate: never reported
        })
        globals_off = {"KOSYNC_ENABLED": "false", "CWA_SYNC_ENABLED": "off", "ABS_ENABLED": "true",
                       "READEST_ENABLED": "false", "READEST_ANNOTATION_SYNC": "false"}
        with patch.dict(os.environ, globals_off):
            r = self.client.get('/api/v1/admin/users/r@example.com', headers=self._h())
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(set(body), {"id", "username", "role", "active", "off_server_wide"})
        self.assertEqual(body["off_server_wide"], ["CWA_SYNC_ENABLED", "KOSYNC_ENABLED"])
        self.assertNotIn("sync secret", r.get_data(as_text=True))

        with patch.dict(os.environ, {"KOSYNC_ENABLED": "true", "CWA_SYNC_ENABLED": "true"}):
            r = self.client.get('/api/v1/admin/users/r@example.com', headers=self._h())
        self.assertEqual(r.get_json()["off_server_wide"], [])
        # A reader with no integrations of their own has nothing switched off.
        self.client.put('/api/v1/admin/users/blank@example.com', headers=self._h(), json={"password": "pw pw pw"})
        with patch.dict(os.environ, globals_off):
            r = self.client.get('/api/v1/admin/users/blank@example.com', headers=self._h())
        self.assertEqual(r.get_json()["off_server_wide"], [])

    # --- end to end: provision, sync, revoke ----------------------------------

    def test_provisioned_reader_can_sync_until_disabled(self):
        key = hashlib.md5(b"reader password").hexdigest()
        self.client.put('/api/v1/admin/users/r@example.com', headers=self._h(), json={"password": "reader password"})
        self.client.patch('/api/v1/admin/users/r@example.com/integrations', headers=self._h(), json={
            "KOSYNC_ENABLED": True, "KOSYNC_USER": "r@example.com", "KOSYNC_KEY": "reader password",
        })
        auth = {"x-auth-user": "r@example.com", "x-auth-key": key}
        with patch.object(kosync_server, "_database_service", self.db):
            self.assertEqual(self.client.get('/users/auth', headers=auth).status_code, 200)
            self.client.put('/api/v1/admin/users/r@example.com', headers=self._h(), json={"active": False})
            self.assertEqual(self.client.get('/users/auth', headers=auth).status_code, 401)

    def test_readest_account_switch_discards_previous_session(self):
        from src.api.readest_client import ReadestClient

        reader = self.db.create_user('readest-reader', 'password')
        self.db.set_user_credentials(reader.id, {
            'READEST_EMAIL': 'old@example.invalid', 'READEST_PASSWORD': 'old-password',
        })
        self.db.set_user_credentials(reader.id, {
            'READEST_ACCESS_TOKEN': 'old-token', 'READEST_REFRESH_TOKEN': 'old-refresh',
            'READEST_TOKEN_EXPIRES_AT': str(time.time() + 3600),
        })
        self.assertEqual(self.db.get_user_credential(reader.id, 'READEST_ACCESS_TOKEN'), 'old-token')
        response = self.client.patch('/api/v1/admin/users/readest-reader/integrations', headers=self._h(),
                                     json={'READEST_EMAIL': 'new@example.invalid', 'READEST_PASSWORD': 'new-password'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['updated'], ['READEST_EMAIL', 'READEST_PASSWORD'])
        client = ReadestClient(self.db.get_user_credentials(reader.id), self.db, reader.id)
        with patch.object(client, 'login', return_value=True) as login:
            self.assertTrue(client.ensure_authenticated())
            login.assert_called_once_with('new@example.invalid', 'new-password')

    def test_integration_patch_rolls_back_all_fields_on_storage_failure(self):
        reader = self.db.create_user('patch-reader', 'password')
        self.db.set_user_credentials(reader.id, {'KOSYNC_USER': 'old-user', 'KOSYNC_KEY': 'old-key'})
        original_store = self.db._store_value

        def fail_second_field(key, value):
            if key == 'KOSYNC_KEY':
                raise RuntimeError('simulated second-field failure')
            return original_store(key, value)

        with patch.object(self.db, '_store_value', side_effect=fail_second_field):
            response = self.client.patch('/api/v1/admin/users/patch-reader/integrations', headers=self._h(),
                                         data='{"KOSYNC_USER":"new-user","KOSYNC_KEY":"new-key"}',
                                         content_type='application/json')
        self.assertEqual(response.status_code, 500)
        self.assertEqual(self.db.get_user_credential(reader.id, 'KOSYNC_USER'), 'old-user')
        admin_api._on_integrations_saved.assert_not_called()

    def test_readest_partial_and_unchanged_updates_handle_sessions(self):
        reader = self.db.create_user('readest-reader', 'password')
        for values, reset in [
            ({'READEST_EMAIL': 'new@example.invalid'}, True),
            ({'READEST_PASSWORD': 'new-password'}, True),
            ({'READEST_EMAIL': 'old@example.invalid'}, False),
            ({'READEST_ENABLED': True}, False),
        ]:
            with self.subTest(values=values):
                self.db.set_user_credentials(reader.id, {'READEST_EMAIL': 'old@example.invalid', 'READEST_PASSWORD': 'old-password'})
                self.db.set_user_credentials(reader.id, {'READEST_ACCESS_TOKEN': 'old-token', 'READEST_REFRESH_TOKEN': 'old-refresh',
                                                         'READEST_TOKEN_EXPIRES_AT': str(time.time() + 3600)})
                response = self.client.patch('/api/v1/admin/users/readest-reader/integrations', headers=self._h(), json=values)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(self.db.get_user_credential(reader.id, 'READEST_ACCESS_TOKEN'), '' if reset else 'old-token')

    def test_failed_readest_token_reset_rolls_back_identity_and_session(self):
        reader = self.db.create_user('readest-reader', 'password')
        self.db.set_user_credentials(reader.id, {'READEST_EMAIL': 'old@example.invalid', 'READEST_PASSWORD': 'old-password'})
        self.db.set_user_credentials(reader.id, {'READEST_ACCESS_TOKEN': 'old-token', 'READEST_REFRESH_TOKEN': 'old-refresh'})
        original_store = self.db._store_value

        def fail_token_reset(key, value):
            if key == 'READEST_REFRESH_TOKEN' and value == '':
                raise RuntimeError('simulated token-reset failure')
            return original_store(key, value)

        with patch.object(self.db, '_store_value', side_effect=fail_token_reset):
            response = self.client.patch('/api/v1/admin/users/readest-reader/integrations', headers=self._h(),
                                         json={'READEST_EMAIL': 'new@example.invalid'})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(self.db.get_user_credential(reader.id, 'READEST_EMAIL'), 'old@example.invalid')
        self.assertEqual(self.db.get_user_credential(reader.id, 'READEST_ACCESS_TOKEN'), 'old-token')

    def test_dashboard_blank_secrets_keep_the_existing_readest_session(self):
        from src import web_server

        reader = self.db.create_user('readest-reader', 'password')
        self.db.set_user_credentials(reader.id, {'READEST_EMAIL': 'old@example.invalid', 'READEST_PASSWORD': 'old-password'})
        self.db.set_user_credentials(reader.id, {'READEST_ACCESS_TOKEN': 'old-token'})
        with patch.object(web_server, 'database_service', self.db), patch.object(web_server, 'container', MagicMock()), \
             self.app.test_request_context('/account/integrations', method='POST',
                                           data={'READEST_EMAIL': 'old@example.invalid', 'READEST_PASSWORD': ''}):
            web_server._apply_user_integrations(reader.id)
        self.assertEqual(self.db.get_user_credential(reader.id, 'READEST_PASSWORD'), 'old-password')
        self.assertEqual(self.db.get_user_credential(reader.id, 'READEST_ACCESS_TOKEN'), 'old-token')

    def test_primary_admin_password_cannot_be_reset_through_the_api(self):
        response = self.client.put('/api/v1/admin/users/admin', headers=self._h(),
                                   json={'role': 'user', 'password': 'replacement-password'})
        self.assertEqual(response.status_code, 403)
        self.assertIsNotNone(self.db.verify_user_credentials('admin', 'admin-password'))

    def test_account_updates_invalidate_cached_client_policy(self):
        reader = self.db.create_user('reader', 'password')
        invalidate = MagicMock()
        admin_api.init_admin_api(self.db, on_user_updated=invalidate)
        response = self.client.put('/api/v1/admin/users/reader', headers=self._h(), json={'role': 'user'})
        self.assertEqual(response.status_code, 200)
        invalidate.assert_called_once_with(reader.id)
        invalidate.reset_mock()
        response = self.client.put('/api/v1/admin/users/reader', headers=self._h(), json={'active': False})
        self.assertEqual(response.status_code, 200)
        invalidate.assert_called_once_with(reader.id)

    def test_concurrent_disables_keep_an_active_admin(self):
        """Both threads reach the UPDATE together unless BEGIN IMMEDIATE serializes them."""
        from sqlalchemy import event

        other = self.db.create_user('other-admin', 'password', role='admin')
        admin = self.db.get_user_by_username('admin')
        barrier = threading.Barrier(2)

        def meet_at_update(conn, cursor, statement, parameters, context, executemany):
            if statement.startswith('UPDATE users'):
                try:
                    barrier.wait(timeout=1)
                except threading.BrokenBarrierError:
                    pass

        engine = self.db.db_manager.engine
        event.listen(engine, 'before_cursor_execute', meet_at_update)
        self.addCleanup(event.remove, engine, 'before_cursor_execute', meet_at_update)

        def disable(user_id):
            try:
                self.db.update_user_account(user_id, active=False)
                return 'disabled'
            except ValueError as e:
                return str(e)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(disable, [admin.id, other.id]))
        self.assertEqual(sorted(results), ["Can't disable the last active admin", 'disabled'])
        self.assertEqual(sum(u.role == 'admin' and bool(u.active) for u in self.db.list_users()), 1)

    def test_reenabled_reader_receives_books_shared_while_inactive(self):
        from src import web_server
        from src.db.models import Book

        reader = self.db.create_user('returning-reader', 'password', active=0)
        self.db.save_book(Book(abs_id='shared-while-disabled', abs_title='Shared', status='active'))
        self.assertFalse(self.db.is_user_linked(reader.id, 'shared-while-disabled'))
        with patch.dict(os.environ, {'SHARE_ALL_BOOKS_WITH_ALL_USERS': 'true'}), patch.object(web_server, 'database_service', self.db):
            admin_api.init_admin_api(self.db, on_user_enabled=web_server._share_library_with_returning_user)
            response = self.client.put('/api/v1/admin/users/returning-reader', headers=self._h(), json={'active': True})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.db.is_user_linked(reader.id, 'shared-while-disabled'))

    # --- hardening --------------------------------------------------------------

    def test_non_ascii_bearer_token_is_401_not_500(self):
        r = self.client.get('/api/v1/admin/users', headers={"Authorization": "Bearer t\u00e9" + "t" * 38})
        self.assertEqual(r.status_code, 401)

    def test_unicode_usernames_round_trip_through_the_api(self):
        r = self.client.put('/api/v1/admin/users/\u00c9mile', headers=self._h(), json={"password": "pw pw pw"})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(self.client.get('/api/v1/admin/users/\u00e9mile', headers=self._h()).status_code, 200)
        r = self.client.put('/api/v1/admin/users/\u00e9mile', headers=self._h(), json={"password": "other pw pw"})
        self.assertEqual((r.status_code, r.get_json()["created"]), (200, False))
        self.assertIsNotNone(self.db.verify_user_credentials('\u00e9MILE', "other pw pw"))

    def test_lost_create_race_becomes_an_update(self):
        real_get = self.db.get_user_by_username
        calls = {"n": 0}

        def stale_first_lookup(name):
            calls["n"] += 1
            return None if calls["n"] == 1 else real_get(name)

        self.db.create_user("racer", "winner password")
        with patch.object(self.db, "get_user_by_username", side_effect=stale_first_lookup):
            r = self.client.put('/api/v1/admin/users/racer', headers=self._h(), json={"password": "loser password"})
        self.assertEqual((r.status_code, r.get_json()["created"]), (200, False))
        self.assertIsNotNone(self.db.verify_user_credentials("racer", "loser password"))

    def test_concurrent_puts_of_the_same_name_create_one_account(self):
        def put(name):
            with self.app.test_client() as client:
                return client.put(f'/api/v1/admin/users/{name}', headers=self._h(), json={"password": "pw pw pw"}).status_code

        with ThreadPoolExecutor(max_workers=4) as pool:
            statuses = list(pool.map(put, ['Bob', 'bob', 'BOB', 'bOb']))
        self.assertNotIn(500, statuses)
        self.assertEqual(statuses.count(201), 1)
        self.assertEqual(sum(u.username.lower() == 'bob' for u in self.db.list_users()), 1)

    def test_repeated_failures_are_rate_limited_and_logged_sanitized(self):
        path = '/api/v1/admin/users/' + 'a' * 500
        with self.assertLogs(admin_api.logger, level="DEBUG") as logs:
            codes = [self.client.get(path, headers=self._h('x' * 40)).status_code for _ in range(7)]
        self.assertEqual(codes, [401] * 5 + [429] * 2)
        self.assertEqual(self.client.get('/api/v1/admin/users', headers=self._h()).status_code, 429)
        rejections = [r for r in logs.records if "rejected request" in r.getMessage()]
        self.assertEqual(len(rejections), 5)
        self.assertLessEqual(sum(r.levelno >= logging.WARNING for r in rejections), 1)
        self.assertTrue(all(len(r.getMessage()) < 300 for r in rejections))

    def test_log_path_has_no_control_characters(self):
        self.assertEqual(admin_api._printable("/a\r\nFAKE\x1b[0m\u00e9", 50), "/a??FAKE?[0m?")
        self.assertEqual(len(admin_api._printable("x" * 999, 120)), 120)

    def test_patch_to_a_deleted_account_is_404(self):
        self.client.put('/api/v1/admin/users/ghost', headers=self._h(), json={"password": "pw pw pw"})
        with patch.object(self.db, "set_user_credentials", return_value=False):
            r = self.client.patch('/api/v1/admin/users/ghost/integrations', headers=self._h(), json={"KOSYNC_USER": "x"})
        self.assertEqual(r.status_code, 404)
        admin_api._on_integrations_saved.assert_not_called()

    def test_reenable_runs_the_enabled_hook_and_account_hook(self):
        reader = self.db.create_user('sleeper', 'password', active=0)
        enabled, updated = MagicMock(), MagicMock()
        admin_api.init_admin_api(self.db, on_user_updated=updated, on_user_enabled=enabled)
        r = self.client.put('/api/v1/admin/users/sleeper', headers=self._h(), json={'active': True})
        self.assertEqual(r.status_code, 200)
        updated.assert_called_once_with(reader.id)
        self.assertEqual(enabled.call_args.args[0].id, reader.id)


if __name__ == '__main__':
    unittest.main()
