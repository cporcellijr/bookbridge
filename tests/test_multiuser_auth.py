import os
import tempfile
import shutil
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from tests.test_webserver import MockContainer
from src.db.database_service import DatabaseService
from src.db.models import Book, KosyncDocument, PendingSuggestion, State
from src.services.koreader_device_sync_service import KOReaderDeviceSyncService

_TEMPLATES = str(Path(__file__).parent.parent / "templates")


class TestFirstRunSetup(unittest.TestCase):
    """First-run auth flow: no default admin password."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ['DATA_DIR'] = self.tmp
        os.environ['BOOKS_DIR'] = self.tmp
        self._orig_template_dir = os.environ.get('TEMPLATE_DIR')
        os.environ['TEMPLATE_DIR'] = _TEMPLATES

        self.svc = DatabaseService(os.path.join(self.tmp, "first-run.db"))
        self.mock_container = MockContainer()
        self.mock_container.mock_database_service = self.svc

        import src.db.migration_utils
        self._orig_init = src.db.migration_utils.initialize_database
        src.db.migration_utils.initialize_database = lambda data_dir: self.svc

        from src.web_server import create_app
        self.app, _ = create_app(test_container=self.mock_container)
        self.app.config['TESTING'] = True
        self.app.config['LOGIN_DISABLED'] = False
        self.client = self.app.test_client()

    def tearDown(self):
        import src.db.migration_utils
        src.db.migration_utils.initialize_database = self._orig_init
        if self._orig_template_dir is None:
            os.environ.pop('TEMPLATE_DIR', None)
        else:
            os.environ['TEMPLATE_DIR'] = self._orig_template_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_first_run_redirects_to_setup(self):
        resp = self.client.get('/', follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/setup', resp.headers.get('Location', ''))

    def test_first_run_api_reports_setup_required(self):
        resp = self.client.get('/api/status', follow_redirects=False)
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.get_json(), {"error": "initial admin setup required"})

    def test_setup_creates_initial_admin_and_logs_in(self):
        self.svc.save_book(Book(abs_id="legacy-book", abs_title="Legacy Book"))
        self.svc.save_state(State(abs_id="legacy-book", client_name="kosync", percentage=0.5))

        resp = self.client.post(
            '/setup',
            data={
                'username': 'cait-admin',
                'password': 'secret123',
                'confirm_password': 'secret123',
            },
            follow_redirects=False,
        )

        self.assertEqual(resp.status_code, 302)
        admin = self.svc.get_user_by_username('cait-admin')
        self.assertIsNotNone(admin)
        self.assertEqual(admin.role, 'admin')
        self.assertIsNotNone(self.svc.verify_user_credentials('cait-admin', 'secret123'))
        with self.client.session_transaction() as sess:
            self.assertEqual(sess.get('user_id'), admin.id)
            self.assertEqual(sess.get('role'), 'admin')
        self.assertEqual(self.svc.get_state("legacy-book", "kosync").user_id, admin.id)

    def test_setup_rejects_password_mismatch(self):
        resp = self.client.post(
            '/setup',
            data={'username': 'admin', 'password': 'one', 'confirm_password': 'two'},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.svc.count_users(), 0)


class TestMultiUserAuth(unittest.TestCase):
    """Auth guard integration: real DatabaseService + auth enabled."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ['DATA_DIR'] = self.tmp
        os.environ['BOOKS_DIR'] = self.tmp
        # Point Flask at the real templates dir (prod uses /app/templates).
        self._orig_template_dir = os.environ.get('TEMPLATE_DIR')
        os.environ['TEMPLATE_DIR'] = _TEMPLATES

        # Real DB with a known admin
        self.svc = DatabaseService(os.path.join(self.tmp, "mu.db"))
        self.svc.create_user("admin", "secret", role="admin")

        self.mock_container = MockContainer()
        self.mock_container.mock_database_service = self.svc  # inject real svc

        import src.db.migration_utils
        self._orig_init = src.db.migration_utils.initialize_database
        src.db.migration_utils.initialize_database = lambda data_dir: self.svc

        from src.web_server import create_app
        self.app, _ = create_app(test_container=self.mock_container)
        self.app.config['TESTING'] = True
        self.app.config['LOGIN_DISABLED'] = False  # enable auth for these tests
        self.client = self.app.test_client()

    def tearDown(self):
        import src.db.migration_utils
        src.db.migration_utils.initialize_database = self._orig_init
        if self._orig_template_dir is None:
            os.environ.pop('TEMPLATE_DIR', None)
        else:
            os.environ['TEMPLATE_DIR'] = self._orig_template_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_login_page_is_public(self):
        self.assertEqual(self.client.get('/login').status_code, 200)

    def test_unauthenticated_html_redirects_to_login(self):
        resp = self.client.get('/', follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login', resp.headers.get('Location', ''))

    def test_unauthenticated_api_returns_401(self):
        resp = self.client.get('/api/status', follow_redirects=False)
        self.assertEqual(resp.status_code, 401)

    def test_bad_login_rejected(self):
        resp = self.client.post('/login', data={'username': 'admin', 'password': 'nope'})
        self.assertEqual(resp.status_code, 401)
        # still not authenticated
        self.assertEqual(self.client.get('/', follow_redirects=False).status_code, 302)

    def test_good_login_grants_access(self):
        resp = self.client.post('/login', data={'username': 'admin', 'password': 'secret'},
                                follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        with self.client.session_transaction() as sess:
            self.assertTrue(sess.get('user_id'))
            self.assertEqual(sess.get('role'), 'admin')
        # session recognized: hitting /login now redirects to index (no dashboard render needed)
        self.assertEqual(self.client.get('/login', follow_redirects=False).status_code, 302)

    def test_login_rejects_protocol_relative_open_redirect(self):
        resp = self.client.post(
            '/login?next=//evil.example.com/x',
            data={'username': 'admin', 'password': 'secret'},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn('evil.example.com', resp.headers.get('Location', ''))

    def test_bind_request_user_context_scopes_global_fallback_flag(self):
        """Regular users must NOT inherit the admin's global env config; admins
        may. The ambient creds carry the resolve_setting fallback flag."""
        import src.web_server as web_server
        from src.utils.user_context import get_current_user_credentials
        from src.utils.user_config import _ALLOW_GLOBAL_FALLBACK_KEY

        admin = self.svc.get_user_by_username('admin')
        reg = self.svc.create_user('reg-iso', 'pw', role='user')

        with self.app.test_request_context('/'):
            web_server._bind_request_user_context(reg)
            self.assertIs(
                get_current_user_credentials().get(_ALLOW_GLOBAL_FALLBACK_KEY), False
            )
            web_server._release_request_user_context()

        with self.app.test_request_context('/'):
            web_server._bind_request_user_context(admin)
            self.assertIs(
                get_current_user_credentials().get(_ALLOW_GLOBAL_FALLBACK_KEY), True
            )
            web_server._release_request_user_context()

    def test_logout_clears_session(self):
        self.client.post('/login', data={'username': 'admin', 'password': 'secret'})
        self.client.post('/logout')
        with self.client.session_transaction() as sess:
            self.assertIsNone(sess.get('user_id'))
        self.assertEqual(self.client.get('/', follow_redirects=False).status_code, 302)

    def _login(self):
        return self.client.post('/login', data={'username': 'admin', 'password': 'secret'})

    def test_account_change_password(self):
        self._login()
        resp = self.client.post('/account', data={
            'current_password': 'secret',
            'new_password': 'newpass1',
            'confirm_password': 'newpass1',
        })
        self.assertEqual(resp.status_code, 200)
        # old password no longer works, new one does
        self.assertIsNone(self.svc.verify_user_credentials('admin', 'secret'))
        self.assertIsNotNone(self.svc.verify_user_credentials('admin', 'newpass1'))

    def test_account_wrong_current_password_rejected(self):
        self._login()
        self.client.post('/account', data={
            'current_password': 'WRONG',
            'new_password': 'x',
            'confirm_password': 'x',
        })
        # password unchanged
        self.assertIsNotNone(self.svc.verify_user_credentials('admin', 'secret'))

    def test_account_change_username(self):
        self._login()
        self.client.post('/account', data={
            'current_password': 'secret',
            'username': 'superadmin',
        })
        self.assertIsNotNone(self.svc.get_user_by_username('superadmin'))
        with self.client.session_transaction() as sess:
            self.assertEqual(sess.get('username'), 'superadmin')

    def test_account_requires_login(self):
        self.assertEqual(self.client.get('/account', follow_redirects=False).status_code, 302)

    def test_regular_user_account_shows_bridgesync_plugin_download(self):
        self.svc.create_user("reg", "pw", role="user")
        self.client.post('/login', data={'username': 'reg', 'password': 'pw'})

        resp = self.client.get('/account')

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'BridgeSync KOReader Plugin', resp.data)
        self.assertIn(b'/api/kosync-plugin/download', resp.data)
        self.assertIn(b'/api/kosync-plugin/version', resp.data)
        self.assertEqual(self.client.get('/api/kosync-plugin/version').status_code, 200)
        self.assertEqual(self.client.get('/api/kosync-plugin/download').status_code, 200)

    def test_regular_user_account_links_to_self_service_integrations(self):
        self.svc.create_user("alice", "pw", role="user")
        self.client.post('/login', data={'username': 'alice', 'password': 'pw'})

        resp = self.client.get('/account')

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'My Integrations', resp.data)
        self.assertIn(b'/account/integrations', resp.data)

    def test_regular_user_integrations_page_shows_bookfusion_link(self):
        self.svc.create_user("alice", "pw", role="user")
        self.client.post('/login', data={'username': 'alice', 'password': 'pw'})

        resp = self.client.get('/account/integrations')

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'My Integrations', resp.data)
        self.assertIn(b'BookFusion', resp.data)
        self.assertIn(b'/api/bookfusion/device/start', resp.data)
        self.assertIn(b'/api/account/test-connection/', resp.data)
        self.assertIn(b'class="toggle-switch"', resp.data)
        self.assertIn(b'name="BOOKFUSION_ENABLED"', resp.data)
        self.assertIn(b'class="group-body collapsed"', resp.data)

    def test_regular_user_can_save_own_integrations(self):
        alice = self.svc.create_user("alice", "pw", role="user")
        self.client.post('/login', data={'username': 'alice', 'password': 'pw'})
        self.svc.set_user_credential(alice.id, 'BOOKFUSION_ACCESS_TOKEN', 'existing-token')

        resp = self.client.post('/account/integrations', data={
            'BOOKFUSION_ENABLED': 'on',
            'BOOKFUSION_ANNOTATION_SYNC': 'on',
            'KOSYNC_ENABLED': 'on',
            'KOSYNC_USER': 'alice-ko',
            'KOSYNC_KEY': '',
        })

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.svc.get_user_credential(alice.id, 'BOOKFUSION_ENABLED'), 'true')
        self.assertEqual(self.svc.get_user_credential(alice.id, 'BOOKFUSION_ANNOTATION_SYNC'), 'true')
        self.assertEqual(self.svc.get_user_credential(alice.id, 'BOOKFUSION_ACCESS_TOKEN'), 'existing-token')
        self.assertEqual(self.svc.get_user_credential(alice.id, 'KOSYNC_ENABLED'), 'true')
        self.assertEqual(self.svc.get_user_credential(alice.id, 'KOSYNC_USER'), 'alice-ko')
        self.mock_container.mock_user_client_registry.invalidate.assert_called_with(alice.id)

    def test_regular_user_links_bookfusion_book_to_own_mapping(self):
        alice = self.svc.create_user("alice-bf", "pw", role="user")
        bob = self.svc.create_user("bob-bf", "pw", role="user")
        self.svc.save_book(Book(
            abs_id="shared-book",
            abs_title="Shared Book",
            status="active",
            duration=100,
            user_id=alice.id,
        ))
        self.svc.link_user_book(bob.id, "shared-book")

        self.client.post('/login', data={'username': 'alice-bf', 'password': 'pw'})
        resp = self.client.post('/api/bookfusion/link/shared-book', json={
            "bookfusion_id": "bf-alice",
            "title": "Alice Copy",
        })

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.svc.resolve_bookfusion_id(alice.id, self.svc.get_book("shared-book")), "bf-alice")
        self.assertIsNone(self.svc.resolve_bookfusion_id(bob.id, self.svc.get_book("shared-book")))

    # --- admin-managed per-user integrations ---
    def _ipath(self, uid):
        return f'/admin/users/{uid}/integrations'

    def test_integrations_requires_login(self):
        u = self.svc.create_user('bob', 'pw', role='user')
        self.assertEqual(self.client.get(self._ipath(u.id), follow_redirects=False).status_code, 302)

    def test_integrations_admin_only(self):
        self.svc.create_user('reg', 'pw', role='user')
        target = self.svc.create_user('bob', 'pw', role='user')
        self.client.post('/login', data={'username': 'reg', 'password': 'pw'})
        self.assertEqual(self.client.get(self._ipath(target.id), follow_redirects=False).status_code, 403)

    def test_batch_match_redirects_to_add_book_for_regular_users(self):
        self.svc.create_user('reg', 'pw', role='user')
        self.client.post('/login', data={'username': 'reg', 'password': 'pw'})
        resp = self.client.get('/batch-match', follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/add-book', resp.headers.get('Location', ''))
        self.assertEqual(self.client.get('/add-book', follow_redirects=False).status_code, 200)

    def test_batch_match_redirects_to_add_book_for_admin(self):
        self._login()  # admin
        resp = self.client.get('/batch-match', follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/add-book', resp.headers.get('Location', ''))

    def test_user_library_lookup_is_admin_only(self):
        self.svc.create_user('reg', 'pw', role='user')
        target = self.svc.create_user('bob', 'pw', role='user')
        self.client.post('/login', data={'username': 'reg', 'password': 'pw'})
        for ep in ('abs-libraries', 'booklore-libraries'):
            resp = self.client.post(f'/api/admin/users/{target.id}/{ep}', json={})
            self.assertEqual(resp.status_code, 403)

    def test_user_abs_library_lookup_unconfigured_returns_400(self):
        target = self.svc.create_user('bob', 'pw', role='user')
        self._login()  # admin
        # The user has no ABS token -> per-user client is unconfigured.
        resp = self.client.post(f'/api/admin/users/{target.id}/abs-libraries', json={})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('not configured', (resp.get_json() or {}).get('error', ''))

    def test_regular_user_integrations_page_warns_no_master_inheritance(self):
        target = self.svc.create_user('bob', 'pw', role='user')
        self._login()
        resp = self.client.get(self._ipath(target.id))
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'Regular users need explicit account credentials', resp.data)
        self.assertNotIn(b'inherit the master Settings', resp.data)
        self.assertIn(b'KOReader Collections', resp.data)
        self.assertIn(b'name="DEVICE_SYNC_COLLECTION_SOURCE"', resp.data)
        self.assertIn(b'value="hardcover"', resp.data)
        self.assertIn(b'name="DEVICE_SYNC_HARDCOVER_LIST_NAMES"', resp.data)
        self.assertIn(b'class="toggle-switch"', resp.data)
        self.assertIn(b'data-source-select="DEVICE_SYNC_COLLECTION_SOURCE"', resp.data)

    def test_admin_saves_user_integrations_and_invalidates(self):
        fake_registry = MagicMock()
        self.mock_container.user_client_registry = MagicMock(return_value=fake_registry)
        target = self.svc.create_user('bob', 'pw', role='user')
        self._login()  # admin
        resp = self.client.post(self._ipath(target.id), data={
            'ABS_KEY': 'bob-abs-token',
            'ABS_LIBRARY_ID': 'bob-lib',
            'STORYTELLER_USER': 'bob',
            'STORYTELLER_PASSWORD': 'secretpw',
            'STORYTELLER_ENABLED': 'on',
            'DEVICE_SYNC_COLLECTION_SOURCE': 'hardcover',
            'DEVICE_SYNC_HARDCOVER_LISTS': 'selected',
            'DEVICE_SYNC_HARDCOVER_LIST_NAMES': 'Owned, Sci-Fi',
        })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.svc.get_user_credential(target.id, 'ABS_KEY'), 'bob-abs-token')
        self.assertEqual(self.svc.get_user_credential(target.id, 'ABS_LIBRARY_ID'), 'bob-lib')
        self.assertEqual(self.svc.get_user_credential(target.id, 'STORYTELLER_ENABLED'), 'true')
        self.assertEqual(self.svc.get_user_credential(target.id, 'KOSYNC_ENABLED'), 'false')
        self.assertEqual(self.svc.get_user_credential(target.id, 'DEVICE_SYNC_COLLECTION_SOURCE'), 'hardcover')
        self.assertEqual(self.svc.get_user_credential(target.id, 'DEVICE_SYNC_HARDCOVER_LISTS'), 'selected')
        self.assertEqual(self.svc.get_user_credential(target.id, 'DEVICE_SYNC_HARDCOVER_LIST_NAMES'), 'Owned, Sci-Fi')
        fake_registry.invalidate.assert_called_once_with(target.id)

    def test_secret_blank_keeps_existing(self):
        self.mock_container.user_client_registry = MagicMock(return_value=MagicMock())
        target = self.svc.create_user('bob', 'pw', role='user')
        self._login()
        self.svc.set_user_credential(target.id, 'ABS_KEY', 'original')
        self.client.post(self._ipath(target.id), data={'ABS_KEY': ''})
        self.assertEqual(self.svc.get_user_credential(target.id, 'ABS_KEY'), 'original')

    def test_text_blank_clears(self):
        self.mock_container.user_client_registry = MagicMock(return_value=MagicMock())
        target = self.svc.create_user('bob', 'pw', role='user')
        self._login()
        self.svc.set_user_credential(target.id, 'STORYTELLER_USER', 'olduser')
        self.client.post(self._ipath(target.id), data={'STORYTELLER_USER': ''})
        self.assertEqual(self.svc.get_user_credential(target.id, 'STORYTELLER_USER'), '')

    @patch('src.web_server.requests.post')
    def test_user_integration_test_uses_saved_secret_when_form_blank(self, mock_post):
        target = self.svc.create_user('caitlin', 'pw', role='user')
        self.svc.set_user_credential(target.id, 'HARDCOVER_ENABLED', 'true')
        self.svc.set_user_credential(target.id, 'HARDCOVER_TOKEN', 'caitlin-token')
        mock_post.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value={"data": {"me": [{"id": 7, "username": "caitlin"}]}}),
        )

        self._login()
        resp = self.client.post(
            f'/api/admin/users/{target.id}/test-connection/hardcover',
            json={'HARDCOVER_ENABLED': True, 'HARDCOVER_TOKEN': ''},
        )

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()['ok'])
        self.assertEqual(mock_post.call_args.kwargs['headers']['Authorization'], 'Bearer caitlin-token')

    @patch('src.web_server.requests.post')
    def test_user_integration_test_regular_user_does_not_inherit_admin_secret(self, mock_post):
        target = self.svc.create_user('caitlin', 'pw', role='user')
        self.svc.set_user_credential(target.id, 'HARDCOVER_ENABLED', 'true')

        self._login()
        with patch.dict(os.environ, {'HARDCOVER_TOKEN': 'admin-token'}, clear=False):
            resp = self.client.post(
                f'/api/admin/users/{target.id}/test-connection/hardcover',
                json={'HARDCOVER_ENABLED': True, 'HARDCOVER_TOKEN': ''},
            )

        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.get_json()['ok'])
        self.assertEqual(resp.get_json()['message'], 'Missing API token')
        mock_post.assert_not_called()

    # --- admin user management (Phase 6c) ---
    def test_admin_users_requires_admin(self):
        # a non-admin user is forbidden
        self.svc.create_user("reg", "pw", role="user")
        self.client.post('/login', data={'username': 'reg', 'password': 'pw'})
        self.assertEqual(self.client.get('/admin/users', follow_redirects=False).status_code, 403)

    def test_admin_can_view_and_create_users(self):
        self.mock_container.user_client_registry = MagicMock(return_value=MagicMock())
        self._login()
        self.assertEqual(self.client.get('/admin/users').status_code, 200)
        self.client.post('/admin/users', data={'action': 'create', 'username': 'newbie', 'password': 'pw', 'role': 'user'})
        created = self.svc.get_user_by_username('newbie')
        self.assertIsNotNone(created)
        self.assertEqual(created.role, 'user')

    def test_cannot_disable_last_admin(self):
        self.mock_container.user_client_registry = MagicMock(return_value=MagicMock())
        self._login()
        uid = self.svc.get_user_by_username('admin').id
        self.client.post('/admin/users', data={'action': 'toggle_active', 'user_id': str(uid)})
        self.assertTrue(self.svc.get_user(uid).active)  # still active

    def test_cannot_delete_self(self):
        self.mock_container.user_client_registry = MagicMock(return_value=MagicMock())
        self._login()
        uid = self.svc.get_user_by_username('admin').id
        self.client.post('/admin/users', data={'action': 'delete', 'user_id': str(uid)})
        self.assertIsNotNone(self.svc.get_user(uid))  # not deleted

    def test_admin_only_pages_forbidden_for_regular_users(self):
        self.svc.create_user("reg", "pw", role="user")
        self.client.post('/login', data={'username': 'reg', 'password': 'pw'})
        for path in ('/settings', '/stats', '/logs', '/suggestions'):
            self.assertEqual(self.client.get(path, follow_redirects=False).status_code, 403,
                             f"{path} should be admin-only")
        # but the home + Add Book flow are reachable
        self.assertNotEqual(self.client.get('/', follow_redirects=False).status_code, 403)
        self.assertNotEqual(self.client.get('/add-book', follow_redirects=False).status_code, 403)

    def test_regular_user_forbidden_from_global_maintenance_apis(self):
        self.svc.create_user("reg", "pw", role="user")
        self.client.post('/login', data={'username': 'reg', 'password': 'pw'})

        checks = [
            ("GET", "/api/kosync-documents"),
            ("POST", "/api/kosync-documents/hash-1/link"),
            ("POST", "/api/kosync-documents/hash-1/unlink"),
            ("DELETE", "/api/kosync-documents/hash-1"),
            ("POST", "/api/restart"),
            ("POST", "/api/test-connection/abs"),
            ("GET", "/api/booklore/libraries"),
            ("GET", "/api/booklore/shelves"),
            ("GET", "/api/abs/libraries"),
            ("POST", "/api/booklore/refresh"),
            ("GET", "/api/alignments/llm-status"),
            ("POST", "/api/alignments/realign"),
        ]
        for method, path in checks:
            resp = self.client.open(path, method=method, json={} if method in ("POST", "DELETE") else None)
            self.assertEqual(resp.status_code, 403, f"{method} {path} should be admin-only")

    def test_test_connection_decorator_remains_admin_only_without_endpoint_guard(self):
        """The global connection tester keeps defense-in-depth authorization."""
        import src.web_server as web_server

        self.svc.create_user("reg", "pw", role="user")
        self.client.post('/login', data={'username': 'reg', 'password': 'pw'})
        was_csrf_enabled = self.app.config['CSRF_ENABLED']
        web_server._ADMIN_ONLY_ENDPOINTS.remove('test_connection')
        self.app.config['CSRF_ENABLED'] = False
        try:
            resp = self.client.post('/api/test-connection/abs', json={})
            self.assertEqual(resp.status_code, 403)
        finally:
            self.app.config['CSRF_ENABLED'] = was_csrf_enabled
            web_server._ADMIN_ONLY_ENDPOINTS.add('test_connection')

    def test_admin_can_reach_kosync_documents_api(self):
        self._login()
        resp = self.client.get('/api/kosync-documents')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["total"], 0)

    def test_regular_user_home_only_shows_their_books(self):
        admin = self.svc.get_user_by_username("admin")
        reg = self.svc.create_user("reg", "pw", role="user")
        self.svc.save_book(Book(abs_id="admin-book", abs_title="Admin Book", ebook_filename="a.epub", duration=100, user_id=admin.id))
        self.svc.save_book(Book(abs_id="reg-book", abs_title="Reg Book", ebook_filename="r.epub", duration=100, user_id=reg.id))
        self.svc.save_state(State(abs_id="admin-book", client_name="kosync", percentage=0.5, user_id=admin.id))
        self.svc.save_state(State(abs_id="reg-book", client_name="kosync", percentage=0.25, user_id=reg.id))

        self.client.post('/login', data={'username': 'reg', 'password': 'pw'})
        resp = self.client.get('/api/status')
        self.assertEqual(resp.status_code, 200)
        ids = [m["abs_id"] for m in resp.get_json()["mappings"]]
        self.assertEqual(ids, ["reg-book"])

    def test_regular_user_home_scopes_by_ownership_not_state(self):
        """Ownership — not a leftover per-user progress row — governs visibility."""
        admin = self.svc.get_user_by_username("admin")
        reg = self.svc.create_user("reg", "pw", role="user")
        self.svc.save_book(Book(abs_id="admin-book", abs_title="Admin Book", ebook_filename="a.epub", duration=100, user_id=admin.id))
        self.svc.save_book(Book(abs_id="reg-book", abs_title="Reg Book", ebook_filename="r.epub", duration=100, user_id=reg.id))
        # reg has a progress row for BOTH books but only owns reg-book.
        self.svc.save_state(State(abs_id="admin-book", client_name="kosync", percentage=0.5, user_id=reg.id))
        self.svc.save_state(State(abs_id="reg-book", client_name="kosync", percentage=0.25, user_id=reg.id))

        self.client.post('/login', data={'username': 'reg', 'password': 'pw'})
        resp = self.client.get('/api/status')
        self.assertEqual(resp.status_code, 200)
        ids = [m["abs_id"] for m in resp.get_json()["mappings"]]
        self.assertEqual(ids, ["reg-book"])

    def test_regular_user_home_shows_owned_processing_book_without_states(self):
        """A freshly matched (processing) book the user owns shows up even before
        any progress row exists — the prior 'matched but invisible' bug."""
        admin = self.svc.get_user_by_username("admin")
        reg = self.svc.create_user("reg", "pw", role="user")
        self.svc.save_book(Book(
            abs_id="processing-book",
            abs_title="Processing Book",
            ebook_filename="p.epub",
            status="processing",
            duration=100,
            user_id=reg.id,
        ))
        self.svc.save_book(Book(
            abs_id="other-processing-book",
            abs_title="Other Processing Book",
            ebook_filename="o.epub",
            status="processing",
            duration=100,
            user_id=admin.id,
        ))

        self.client.post('/login', data={'username': 'reg', 'password': 'pw'})
        resp = self.client.get('/api/status')
        self.assertEqual(resp.status_code, 200)
        ids = [m["abs_id"] for m in resp.get_json()["mappings"]]
        self.assertEqual(ids, ["processing-book"])
        self.assertEqual(resp.get_json()["mappings"][0]["status"], "processing")

    def test_admin_home_scoped_to_own_matches(self):
        """Admins are scoped to their own matches too (no operator-wide view)."""
        admin = self.svc.get_user_by_username("admin")
        reg = self.svc.create_user("reg", "pw", role="user")
        self.svc.save_book(Book(abs_id="admin-book", abs_title="Admin Book", ebook_filename="a.epub", duration=100, user_id=admin.id))
        self.svc.save_book(Book(abs_id="reg-book", abs_title="Reg Book", ebook_filename="r.epub", duration=100, user_id=reg.id))
        self.svc.save_state(State(abs_id="admin-book", client_name="kosync", percentage=0.5, user_id=admin.id))
        self.svc.save_state(State(abs_id="reg-book", client_name="kosync", percentage=0.25, user_id=reg.id))

        self._login()
        resp = self.client.get('/api/status')
        self.assertEqual(resp.status_code, 200)
        ids = {m["abs_id"] for m in resp.get_json()["mappings"]}
        self.assertEqual(ids, {"admin-book"})
        self.assertNotEqual(self.client.get('/match', follow_redirects=False).status_code, 403)

    def test_dashboard_integrations_use_logged_in_user_bundle(self):
        """Dashboard service rows should reflect the logged-in user's tracker
        credentials, not the admin/global tracker clients."""
        reg = self.svc.create_user("caitlin", "pw", role="user")
        self.svc.save_book(Book(
            abs_id="caitlin-book",
            abs_title="Caitlin Book",
            ebook_filename="c.epub",
            status="active",
            duration=100,
            user_id=reg.id,
        ))

        global_clients = {
            "Hardcover": MagicMock(is_configured=MagicMock(return_value=False)),
            "StoryGraph": MagicMock(is_configured=MagicMock(return_value=True)),
        }
        self.mock_container.mock_sync_clients.items.return_value = global_clients.items()

        user_clients = {
            "Hardcover": MagicMock(is_configured=MagicMock(return_value=True)),
            "StoryGraph": MagicMock(is_configured=MagicMock(return_value=False)),
        }
        registry = MagicMock()
        registry.get_clients.return_value = SimpleNamespace(sync_clients=user_clients)
        self.mock_container.user_client_registry = MagicMock(return_value=registry)

        self.client.post('/login', data={'username': 'caitlin', 'password': 'pw'})

        import src.web_server
        original_render = src.web_server.render_template
        mock_render = MagicMock(return_value="dashboard")
        src.web_server.render_template = mock_render
        try:
            resp = self.client.get('/')
        finally:
            src.web_server.render_template = original_render

        self.assertEqual(resp.status_code, 200)
        integrations = mock_render.call_args.kwargs["integrations"]
        self.assertTrue(integrations["hardcover"])
        self.assertFalse(integrations["storygraph"])
        registry.get_clients.assert_called_with(reg.id)

    def test_hardcover_resolve_uses_logged_in_user_bundle(self):
        reg = self.svc.create_user("caitlin", "pw", role="user")
        self.svc.save_book(Book(
            abs_id="caitlin-book",
            abs_title="Caitlin Book",
            ebook_filename="c.epub",
            status="active",
            duration=100,
            user_id=reg.id,
        ))

        hardcover_client = MagicMock()
        hardcover_client.is_configured.return_value = True
        hardcover_client.resolve_book_from_input.return_value = {
            "book_id": 123,
            "title": "Caitlin Book",
            "slug": "caitlin-book",
        }
        hardcover_client.get_book_editions.return_value = []
        hardcover_client.get_book_author.return_value = "Author"

        registry = MagicMock()
        registry.get_clients.return_value = SimpleNamespace(
            hardcover_client=hardcover_client,
            abs_client=MagicMock(),
        )
        self.mock_container.user_client_registry = MagicMock(return_value=registry)

        self.client.post('/login', data={'username': 'caitlin', 'password': 'pw'})
        resp = self.client.get('/api/hardcover/resolve?abs_id=caitlin-book&input=https://hardcover.app/books/123')

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["found"])
        registry.get_clients.assert_called_with(reg.id)
        hardcover_client.is_configured.assert_called()

    def test_regular_user_cannot_mutate_unclaimed_book_routes(self):
        admin = self.svc.get_user_by_username("admin")
        self.svc.save_book(Book(
            abs_id="admin-book",
            abs_title="Admin Book",
            ebook_filename="a.epub",
            kosync_doc_id="old-hash",
            storyteller_uuid="st-uuid",
            status="active",
            user_id=admin.id,
        ))
        reg = self.svc.create_user("reg", "pw", role="user")
        self.client.post('/login', data={'username': 'reg', 'password': 'pw'})

        with patch("src.web_server.threading.Thread") as thread_cls:
            self.assertEqual(self.client.post('/api/sync-now/admin-book').status_code, 403)
            thread_cls.assert_not_called()

        self.assertEqual(
            self.client.post('/update-hash/admin-book', data={'new_hash': 'new-hash'}).status_code,
            403,
        )
        self.assertEqual(self.svc.get_book("admin-book").kosync_doc_id, "old-hash")

        self.assertEqual(
            self.client.post('/api/storyteller/link/admin-book', json={'uuid': 'none'}).status_code,
            403,
        )
        self.assertEqual(self.svc.get_book("admin-book").storyteller_uuid, "st-uuid")

        self.assertEqual(
            self.client.get('/api/hardcover/resolve?abs_id=admin-book&input=123').status_code,
            403,
        )
        self.assertEqual(
            self.client.post('/link-hardcover/admin-book', json={'book_id': 123}).status_code,
            403,
        )
        self.assertEqual(
            self.client.get('/api/storygraph/resolve?abs_id=admin-book&input=123').status_code,
            403,
        )
        self.assertEqual(
            self.client.post('/link-storygraph/admin-book', json={'book_id': 123}).status_code,
            403,
        )
        self.assertEqual(self.svc.get_book_user_ids("admin-book"), [admin.id])
        self.assertEqual(reg.role, "user")

    def test_regular_user_can_mutate_claimed_book_routes(self):
        reg = self.svc.create_user("reg", "pw", role="user")
        self.svc.save_book(Book(
            abs_id="reg-book",
            abs_title="Reg Book",
            ebook_filename="r.epub",
            kosync_doc_id="old-hash",
            storyteller_uuid="st-uuid",
            status="active",
            user_id=reg.id,
        ))
        self.client.post('/login', data={'username': 'reg', 'password': 'pw'})

        with patch("src.web_server.threading.Thread") as thread_cls:
            resp = self.client.post('/api/sync-now/reg-book')
            self.assertEqual(resp.status_code, 200)
            thread_cls.assert_called_once()
            self.assertIs(thread_cls.call_args.kwargs["target"], self.mock_container.mock_sync_manager.sync_cycle)
            self.assertEqual(
                thread_cls.call_args.kwargs["kwargs"],
                {'target_abs_id': 'reg-book', 'user_id': reg.id},
            )

        self.assertEqual(
            self.client.post('/update-hash/reg-book', data={'new_hash': 'new-hash'}).status_code,
            302,
        )
        self.assertEqual(self.svc.get_book("reg-book").kosync_doc_id, "new-hash")
        # The pinned hash is registered as a durable linked KosyncDocument sibling so the
        # device-sync reconciler / re-match can't strand it (issue #285).
        linked_doc = self.svc.get_kosync_document("new-hash")
        self.assertIsNotNone(linked_doc)
        self.assertEqual(linked_doc.linked_abs_id, "reg-book")
        previous_doc = self.svc.get_kosync_document("old-hash")
        self.assertIsNotNone(previous_doc)
        self.assertEqual(previous_doc.linked_abs_id, "reg-book")

        resp = self.client.post('/api/storyteller/link/reg-book', json={'uuid': 'none'})
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(self.svc.get_book("reg-book").storyteller_uuid)

    def test_manual_hash_survives_device_manifest_rebuild(self):
        """A manifest refresh must not replace a hash explicitly pinned by a user."""
        reg = self.svc.create_user("reg-hash", "pw", role="user")
        ebook_path = Path(self.tmp) / "manual-hash.epub"
        ebook_path.write_bytes(b"issue-316-epub")
        self.svc.save_book(Book(
            abs_id="manual-hash-book",
            abs_title="Manual Hash Book",
            ebook_filename=ebook_path.name,
            original_ebook_filename=ebook_path.name,
            kosync_doc_id="original-content-hash",
            status="active",
            user_id=reg.id,
        ))
        self.client.post('/login', data={'username': 'reg-hash', 'password': 'pw'})

        with patch("src.web_server.threading.Thread"):
            response = self.client.post(
                '/update-hash/manual-hash-book',
                data={'new_hash': 'manually-pinned-hash'},
            )
        self.assertEqual(response.status_code, 302)

        ebook_parser = MagicMock()
        ebook_parser.resolve_book_path.return_value = ebook_path
        ebook_parser.get_kosync_id.return_value = "original-content-hash"
        service = KOReaderDeviceSyncService(
            database_service=self.svc,
            ebook_parser=ebook_parser,
            abs_client=MagicMock(),
            booklore_client=MagicMock(),
            cwa_client=MagicMock(),
            epub_cache_dir=Path(self.tmp) / "epub_cache",
        )

        manifest = service.build_manifest()

        self.assertEqual(
            self.svc.get_book("manual-hash-book").kosync_doc_id,
            "manually-pinned-hash",
        )
        linked_hashes = {
            doc.document_hash
            for doc in self.svc.get_kosync_documents_for_book("manual-hash-book")
        }
        self.assertEqual(
            linked_hashes,
            {"original-content-hash", "manually-pinned-hash"},
        )
        self.assertEqual(manifest["books"][0]["content_hash"], "original-content-hash")

    def test_regular_user_kosync_documents_are_scoped_to_own_and_claimed(self):
        admin = self.svc.get_user_by_username("admin")
        reg = self.svc.create_user("reg", "pw", role="user")
        self.svc.save_book(Book(
            abs_id="admin-book",
            abs_title="Admin Book",
            ebook_filename="a.epub",
            status="active",
            user_id=admin.id,
        ))
        self.svc.save_book(Book(
            abs_id="reg-book",
            abs_title="Reg Book",
            ebook_filename="r.epub",
            status="active",
            user_id=reg.id,
        ))
        self.svc.save_kosync_document(KosyncDocument(
            document_hash="a" * 32,
            percentage=0.1,
            user_id=admin.id,
        ))
        self.svc.save_kosync_document(KosyncDocument(
            document_hash="b" * 32,
            percentage=0.2,
            user_id=reg.id,
        ))
        self.svc.save_kosync_document(KosyncDocument(
            document_hash="c" * 32,
            percentage=0.3,
            linked_abs_id="reg-book",
            user_id=admin.id,
        ))

        self.client.post('/login', data={'username': 'reg', 'password': 'pw'})
        resp = self.client.get('/api/me/kosync-documents')
        self.assertEqual(resp.status_code, 200)
        hashes = {doc["document_hash"] for doc in resp.get_json()["documents"]}
        self.assertEqual(hashes, {"b" * 32, "c" * 32})

        books_resp = self.client.get('/api/me/books')
        self.assertEqual(books_resp.status_code, 200)
        self.assertEqual(
            {book["abs_id"] for book in books_resp.get_json()["books"]},
            {"reg-book"},
        )

    def test_regular_user_links_kosync_document_without_replacing_primary_hash(self):
        admin = self.svc.get_user_by_username("admin")
        reg = self.svc.create_user("reg", "pw", role="user")
        self.svc.save_book(Book(
            abs_id="admin-book",
            abs_title="Admin Book",
            ebook_filename="a.epub",
            status="active",
            user_id=admin.id,
        ))
        self.svc.save_book(Book(
            abs_id="reg-book",
            abs_title="Reg Book",
            ebook_filename="r.epub",
            kosync_doc_id="primary-hash",
            status="active",
            user_id=reg.id,
        ))
        self.svc.save_kosync_document(KosyncDocument(
            document_hash="d" * 32,
            percentage=0.4,
            user_id=reg.id,
        ))

        self.client.post('/login', data={'username': 'reg', 'password': 'pw'})
        forbidden = self.client.post(
            f"/api/me/kosync-documents/{'d' * 32}/link",
            json={"abs_id": "admin-book"},
        )
        self.assertEqual(forbidden.status_code, 403)

        resp = self.client.post(
            f"/api/me/kosync-documents/{'d' * 32}/link",
            json={"abs_id": "reg-book"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["success"])
        self.assertEqual(self.svc.get_kosync_document("d" * 32).linked_abs_id, "reg-book")
        self.assertEqual(self.svc.get_book("reg-book").kosync_doc_id, "primary-hash")

    def test_regular_user_can_unlink_and_delete_only_allowed_kosync_documents(self):
        reg = self.svc.create_user("reg", "pw", role="user")
        other = self.svc.create_user("other", "pw", role="user")
        self.svc.save_book(Book(
            abs_id="reg-book",
            abs_title="Reg Book",
            ebook_filename="r.epub",
            status="active",
            user_id=reg.id,
        ))
        self.svc.save_kosync_document(KosyncDocument(
            document_hash="e" * 32,
            linked_abs_id="reg-book",
            user_id=reg.id,
        ))
        self.svc.save_kosync_document(KosyncDocument(
            document_hash="f" * 32,
            user_id=reg.id,
        ))
        self.svc.save_kosync_document(KosyncDocument(
            document_hash="9" * 32,
            user_id=other.id,
        ))

        self.client.post('/login', data={'username': 'reg', 'password': 'pw'})
        delete_linked = self.client.delete(f"/api/me/kosync-documents/{'e' * 32}")
        self.assertEqual(delete_linked.status_code, 400)
        unlink = self.client.post(f"/api/me/kosync-documents/{'e' * 32}/unlink")
        self.assertEqual(unlink.status_code, 200)
        self.assertIsNone(self.svc.get_kosync_document("e" * 32).linked_abs_id)

        delete_own = self.client.delete(f"/api/me/kosync-documents/{'f' * 32}")
        self.assertEqual(delete_own.status_code, 200)
        self.assertIsNone(self.svc.get_kosync_document("f" * 32))

        delete_other = self.client.delete(f"/api/me/kosync-documents/{'9' * 32}")
        self.assertEqual(delete_other.status_code, 403)
        self.assertIsNotNone(self.svc.get_kosync_document("9" * 32))

    def test_regular_user_dashboard_hides_pending_suggestions(self):
        admin = self.svc.get_user_by_username("admin")
        reg = self.svc.create_user("reg", "pw", role="user")
        self.svc.save_book(Book(
            abs_id="reg-book",
            abs_title="Reg Book",
            ebook_filename="r.epub",
            status="active",
            duration=100,
            user_id=reg.id,
        ))
        self.svc.save_pending_suggestion(PendingSuggestion(
            source_id="admin-audio",
            title="Admin Suggestion",
            matches_json='[{"filename": "secret.epub", "score": 99}]',
        ))

        import src.web_server
        original_render = src.web_server.render_template
        mock_render = MagicMock(return_value="dashboard")
        src.web_server.render_template = mock_render
        try:
            self.client.post('/login', data={'username': 'reg', 'password': 'pw'})
            self.assertEqual(self.client.get('/').status_code, 200)
            self.assertEqual(mock_render.call_args.kwargs["suggestions"], [])

            self.client.post('/logout')
            self._login()
            self.assertEqual(self.client.get('/').status_code, 200)
            admin_suggestions = mock_render.call_args.kwargs["suggestions"]
            self.assertEqual([s.source_id for s in admin_suggestions], ["admin-audio"])
        finally:
            src.web_server.render_template = original_render
        self.assertEqual(admin.role, "admin")

    def test_save_book_stamps_owner_from_context_and_preserves_on_update(self):
        from src.utils.user_context import set_current_user_id, reset_current_user_id
        admin = self.svc.get_user_by_username("admin")
        reg = self.svc.create_user("reg", "pw", role="user")

        # No ambient user context -> the match defaults to the admin owner.
        b1 = self.svc.save_book(Book(abs_id="b1", abs_title="B1", ebook_filename="b1.epub"))
        self.assertEqual(b1.user_id, admin.id)

        # A request/sync running as reg stamps reg as the owner.
        tok = set_current_user_id(reg.id)
        try:
            b2 = self.svc.save_book(Book(abs_id="b2", abs_title="B2", ebook_filename="b2.epub"))
        finally:
            reset_current_user_id(tok)
        self.assertEqual(b2.user_id, reg.id)

        # A later update (carrying no owner) must never reassign ownership.
        updated = self.svc.save_book(Book(abs_id="b2", abs_title="B2 v2", ebook_filename="b2.epub"))
        self.assertEqual(updated.user_id, reg.id)

        # Owner-scoped accessors.
        self.assertEqual({b.abs_id for b in self.svc.get_all_books(user_id=reg.id)}, {"b2"})
        self.assertEqual({b.abs_id for b in self.svc.get_books_by_status("active", user_id=admin.id)}, {"b1"})

    def test_admin_can_reach_admin_pages(self):
        self.mock_container.user_client_registry = MagicMock(return_value=MagicMock())
        self._login()
        self.assertEqual(self.client.get('/settings', follow_redirects=False).status_code, 200)

    def test_admin_reset_password(self):
        self.mock_container.user_client_registry = MagicMock(return_value=MagicMock())
        u = self.svc.create_user("reg2", "old", role="user")
        self._login()
        self.client.post('/admin/users', data={'action': 'reset_password', 'user_id': str(u.id), 'password': 'fresh'})
        self.assertIsNotNone(self.svc.verify_user_credentials('reg2', 'fresh'))

    def test_book_can_be_claimed_by_two_users(self):
        """Shared catalog: the same Book row can be matched/claimed by two users,
        and shows on each of their dashboards independently."""
        admin = self.svc.get_user_by_username("admin")
        reg = self.svc.create_user("reg", "pw", role="user")
        # Admin matches the book (save_book links the creator).
        self.svc.save_book(Book(abs_id="shared", abs_title="Shared", ebook_filename="s.epub",
                                status="active", user_id=admin.id))
        # Caitlin/reg claims the SAME book.
        self.svc.link_user_book(reg.id, "shared")

        self.assertEqual({b.abs_id for b in self.svc.get_all_books(user_id=admin.id)}, {"shared"})
        self.assertEqual({b.abs_id for b in self.svc.get_all_books(user_id=reg.id)}, {"shared"})
        self.assertEqual(sorted(self.svc.get_book_user_ids("shared")), sorted([admin.id, reg.id]))

        # reg drops their claim -> book remains visible to admin only.
        self.assertEqual(self.svc.unlink_user_book(reg.id, "shared"), 1)
        self.assertEqual({b.abs_id for b in self.svc.get_all_books(user_id=reg.id)}, set())
        self.assertEqual({b.abs_id for b in self.svc.get_all_books(user_id=admin.id)}, {"shared"})

    def test_delete_mapping_only_unlinks_when_others_claim(self):
        """Deleting a shared book just drops the current user's claim while another
        user still has it; the catalog row survives."""
        self.mock_container.user_client_registry = MagicMock(return_value=MagicMock())
        admin = self.svc.get_user_by_username("admin")
        reg = self.svc.create_user("reg", "pw", role="user")
        self.svc.save_book(Book(abs_id="shared", abs_title="Shared", ebook_filename="s.epub",
                                status="active", user_id=admin.id))
        self.svc.link_user_book(reg.id, "shared")

        self._login()  # admin
        self.client.post('/delete/shared')

        self.assertIsNotNone(self.svc.get_book("shared"))  # row survives
        self.assertEqual(self.svc.get_book_user_ids("shared"), [reg.id])  # only reg left

    def test_session_secret_key_is_not_the_static_fallback(self):
        # The old hardcoded signing key let anyone forge an admin session cookie.
        self.assertTrue(self.app.secret_key)
        self.assertNotEqual(self.app.secret_key, "kosync-queue-secret-unified-app")

    def test_regular_user_cannot_destroy_another_users_book(self):
        admin = self.svc.get_user_by_username("admin")
        self.svc.save_book(Book(abs_id="admin-book", abs_title="Admin Book",
                                ebook_filename="a.epub", status="active", user_id=admin.id))
        self.svc.create_user("reg", "pw", role="user")
        self.client.post('/login', data={'username': 'reg', 'password': 'pw'})  # not a claimant

        self.assertEqual(self.client.post('/delete/admin-book', follow_redirects=False).status_code, 403)
        self.assertEqual(self.client.post('/clear-progress/admin-book', follow_redirects=False).status_code, 403)
        self.assertEqual(self.client.post('/api/mark-complete/admin-book', json={}).status_code, 403)
        # The admin's book is untouched.
        self.assertIsNotNone(self.svc.get_book("admin-book"))
        self.assertEqual(self.svc.get_book_user_ids("admin-book"), [admin.id])

    def test_kosync_blueprint_is_exempt_from_web_login(self):
        # Device sync endpoint must NOT be redirected to the web login page.
        resp = self.client.get('/users/auth', follow_redirects=False)
        self.assertNotEqual(resp.status_code, 302)
        self.assertNotIn('/login', resp.headers.get('Location', '') or '')


class TestCsrfProtection(unittest.TestCase):
    """CSRF guard: session-authenticated mutations require a token; header-authed
    and unauthenticated requests are not in scope."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ['DATA_DIR'] = self.tmp
        os.environ['BOOKS_DIR'] = self.tmp
        self._orig_template_dir = os.environ.get('TEMPLATE_DIR')
        os.environ['TEMPLATE_DIR'] = _TEMPLATES

        self.svc = DatabaseService(os.path.join(self.tmp, "csrf.db"))
        self.svc.create_user("admin", "secret", role="admin")
        self.mock_container = MockContainer()
        self.mock_container.mock_database_service = self.svc

        import src.db.migration_utils
        self._orig_init = src.db.migration_utils.initialize_database
        src.db.migration_utils.initialize_database = lambda data_dir: self.svc

        from src.web_server import create_app
        self.app, _ = create_app(test_container=self.mock_container)
        self.app.config['TESTING'] = True
        self.app.config['LOGIN_DISABLED'] = False
        self.app.config['CSRF_ENABLED'] = True  # the harness disables it by default
        self.client = self.app.test_client()

    def tearDown(self):
        import src.db.migration_utils
        src.db.migration_utils.initialize_database = self._orig_init
        if self._orig_template_dir is None:
            os.environ.pop('TEMPLATE_DIR', None)
        else:
            os.environ['TEMPLATE_DIR'] = self._orig_template_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _login(self):
        return self.client.post('/login', data={'username': 'admin', 'password': 'secret'})

    def _token(self):
        # GET an authed HTML page so the after_request injector seeds the token.
        self.client.get('/account')
        with self.client.session_transaction() as sess:
            return sess.get('_csrf_token')

    def _pw_change(self):
        return {'current_password': 'secret', 'new_password': 'np123456', 'confirm_password': 'np123456'}

    def test_authed_post_without_token_blocked(self):
        self._login()
        resp = self.client.post('/account', data=self._pw_change())
        self.assertEqual(resp.status_code, 403)

    def test_authed_post_with_header_token_allowed(self):
        self._login()
        token = self._token()
        self.assertTrue(token)
        resp = self.client.post('/account', data=self._pw_change(),
                                headers={'X-CSRF-Token': token})
        self.assertNotEqual(resp.status_code, 403)

    def test_authed_post_with_form_token_allowed(self):
        self._login()
        token = self._token()
        data = self._pw_change()
        data['csrf_token'] = token
        resp = self.client.post('/account', data=data)
        self.assertNotEqual(resp.status_code, 403)

    def test_bootstrap_injected_into_authed_html(self):
        self._login()
        resp = self.client.get('/account')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('X-CSRF-Token', resp.get_data(as_text=True))

    def test_unauthenticated_post_not_csrf_blocked(self):
        # No session -> the auth guard redirects to login; CSRF never fires (403).
        resp = self.client.post('/account', data={'current_password': 'x'},
                                follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login', resp.headers.get('Location', ''))


class TestCoverProxyUserIsolation(unittest.TestCase):
    """T1-a: Cover proxy routes must check book ownership."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ['DATA_DIR'] = self.tmp
        os.environ['BOOKS_DIR'] = self.tmp
        self._orig_template_dir = os.environ.get('TEMPLATE_DIR')
        os.environ['TEMPLATE_DIR'] = _TEMPLATES

        self.svc = DatabaseService(os.path.join(self.tmp, "cover-proxy.db"))
        self.svc.create_user("admin", "secret", role="admin")
        self.reg = self.svc.create_user("reg", "pw", role="user")

        self.mock_container = MockContainer()
        self.mock_container.mock_database_service = self.svc
        self.mock_container.mock_booklore_client.is_configured.return_value = True
        self.mock_container.mock_booklore_client.get_audiobook_cover_bytes.return_value = (
            b"booklore-cover", "image/jpeg",
        )
        self.mock_container.mock_bookorbit_client.is_configured.return_value = True
        self.mock_container.mock_bookorbit_client.get_cover_bytes.return_value = (
            b"bookorbit-cover", "image/jpeg",
        )
        self.mock_container.mock_user_client_registry.get_clients.return_value = SimpleNamespace(
            bookorbit_client=self.mock_container.mock_bookorbit_client,
        )

        import src.db.migration_utils
        self._orig_init = src.db.migration_utils.initialize_database
        src.db.migration_utils.initialize_database = lambda data_dir: self.svc

        from src.web_server import create_app
        self.app, _ = create_app(test_container=self.mock_container)
        self.app.config['TESTING'] = True
        self.app.config['LOGIN_DISABLED'] = False
        self.client = self.app.test_client()

        # Create two books, each owned by a different user
        admin = self.svc.get_user_by_username("admin")
        self.svc.save_book(Book(
            abs_id="admin-book", abs_title="Admin Book",
            ebook_filename="a.epub", duration=100, user_id=admin.id,
        ))
        self.svc.save_book(Book(
            abs_id="reg-book", abs_title="Reg Book",
            ebook_filename="r.epub", duration=100, user_id=self.reg.id,
        ))
        self.svc.save_book(Book(
            abs_id="reg-booklore-audio", abs_title="Reg Grimmory Audio",
            audio_source="BookLore", audio_source_id="bl-audio-1",
            ebook_source="BookFusion", ebook_source_id="bf-text-1",
            ebook_filename="bl-audio.epub", duration=100, user_id=self.reg.id,
        ))
        self.svc.save_book(Book(
            abs_id="reg-bookorbit-audio", abs_title="Reg BookOrbit Audio",
            audio_source="BookOrbit", audio_source_id="bo-audio-1",
            ebook_source="Storyteller", ebook_source_id="st-text-1",
            ebook_filename="bo-audio.epub", duration=100, user_id=self.reg.id,
        ))
        self.svc.link_user_book(admin.id, "admin-book")
        self.svc.link_user_book(self.reg.id, "reg-book")
        self.svc.link_user_book(self.reg.id, "reg-booklore-audio")
        self.svc.link_user_book(self.reg.id, "reg-bookorbit-audio")

    def tearDown(self):
        import src.db.migration_utils
        src.db.migration_utils.initialize_database = self._orig_init
        if self._orig_template_dir is None:
            os.environ.pop('TEMPLATE_DIR', None)
        else:
            os.environ['TEMPLATE_DIR'] = self._orig_template_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _login(self, username="admin", password="secret"):
        return self.client.post('/login', data={'username': username, 'password': password})

    def test_regular_user_cannot_proxy_other_users_book_cover(self):
        """Non-owner gets 403 when requesting another user's book cover."""
        self._login("reg", "pw")
        resp = self.client.get('/api/cover-proxy/admin-book')
        self.assertEqual(resp.status_code, 403)
        data = resp.get_json()
        self.assertIsNotNone(data)
        self.assertFalse(data.get("success", True))

    def test_regular_user_can_proxy_own_book_cover(self):
        """Owner passes the ownership gate for their own book."""
        self._login("reg", "pw")
        resp = self.client.get('/api/cover-proxy/reg-book')
        # Gate passes; ABS not configured in test → 500, not 403
        self.assertNotEqual(resp.status_code, 403)

    def test_admin_can_proxy_any_book_cover(self):
        """Admin passes the ownership gate for any book."""
        self._login("admin", "secret")
        resp = self.client.get('/api/cover-proxy/admin-book')
        self.assertNotEqual(resp.status_code, 403)
        resp = self.client.get('/api/cover-proxy/reg-book')
        self.assertNotEqual(resp.status_code, 403)

    def test_regular_user_can_proxy_owned_booklore_audio_with_other_ebook_source(self):
        """Audio ownership resolves independently from the linked ebook source."""
        self._login("reg", "pw")
        resp = self.client.get('/api/booklore/audiobook-cover/bl-audio-1')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, b"booklore-cover")

    def test_regular_user_can_proxy_owned_bookorbit_audio_with_other_ebook_source(self):
        """BookOrbit audio ids are not looked up through ebook source fields."""
        self._login("reg", "pw")
        resp = self.client.get('/api/bookorbit/audiobook-cover/bo-audio-1')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, b"bookorbit-cover")


class TestForgeActiveUserIsolation(unittest.TestCase):
    """T1-b: /api/forge/active must scope results per user."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ['DATA_DIR'] = self.tmp
        os.environ['BOOKS_DIR'] = self.tmp
        self._orig_template_dir = os.environ.get('TEMPLATE_DIR')
        os.environ['TEMPLATE_DIR'] = _TEMPLATES

        self.svc = DatabaseService(os.path.join(self.tmp, "forge-active.db"))
        self.svc.create_user("admin", "secret", role="admin")
        self.reg = self.svc.create_user("reg", "pw", role="user")

        self.mock_container = MockContainer()
        self.mock_container.mock_database_service = self.svc

        import src.db.migration_utils
        self._orig_init = src.db.migration_utils.initialize_database
        src.db.migration_utils.initialize_database = lambda data_dir: self.svc

        from src.web_server import create_app
        self.app, _ = create_app(test_container=self.mock_container)
        self.app.config['TESTING'] = True
        self.app.config['LOGIN_DISABLED'] = False
        self.client = self.app.test_client()

        admin = self.svc.get_user_by_username("admin")
        # forging books: one owned by admin, one by reg
        self.svc.save_book(Book(
            abs_id="admin-forging", abs_title="Admin Forging",
            status="forging", duration=100, user_id=admin.id,
        ))
        self.svc.save_book(Book(
            abs_id="reg-forging", abs_title="Reg Forging",
            status="forging", duration=100, user_id=self.reg.id,
        ))
        self.svc.link_user_book(admin.id, "admin-forging")
        self.svc.link_user_book(self.reg.id, "reg-forging")

    def tearDown(self):
        import src.db.migration_utils
        src.db.migration_utils.initialize_database = self._orig_init
        if self._orig_template_dir is None:
            os.environ.pop('TEMPLATE_DIR', None)
        else:
            os.environ['TEMPLATE_DIR'] = self._orig_template_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _login(self, username="admin", password="secret"):
        return self.client.post('/login', data={'username': username, 'password': password})

    def test_regular_user_sees_only_own_forging_books(self):
        """Regular user's forge/active must only list books they own."""
        self._login("reg", "pw")
        resp = self.client.get('/api/forge/active')
        self.assertEqual(resp.status_code, 200)
        titles = resp.get_json()
        self.assertIsInstance(titles, list)
        self.assertIn("Reg Forging", titles)
        self.assertNotIn("Admin Forging", titles)

    def test_admin_sees_all_forging_books(self):
        """Admin's forge/active must list all forging books."""
        self._login("admin", "secret")
        resp = self.client.get('/api/forge/active')
        self.assertEqual(resp.status_code, 200)
        titles = resp.get_json()
        self.assertIsInstance(titles, list)
        self.assertIn("Admin Forging", titles)
        self.assertIn("Reg Forging", titles)


class TestResolveUidFallbackWarning(unittest.TestCase):
    """T2-a: _resolve_uid(None) must log a warning when no contextvar is set."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ['DATA_DIR'] = self.tmp
        os.environ['BOOKS_DIR'] = self.tmp

        self.svc = DatabaseService(os.path.join(self.tmp, "resolve-uid.db"))
        self.svc.create_user("admin", "secret", role="admin")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_resolve_uid_none_logs_warning_when_no_context(self):
        """Calling _resolve_uid(None) without ambient context must emit a warning."""
        import logging
        from src.utils.user_context import get_current_user_id

        # Ensure NO ambient context is set
        self.assertIsNone(get_current_user_id())

        with self.assertLogs(level=logging.WARNING) as log_cm:
            uid = self.svc._resolve_uid(None)
            self.assertIsNotNone(uid)  # still returns default admin id
            self.assertTrue(
                any("falling back to _default_user_id()" in msg for msg in log_cm.output),
                msg=f"Expected fallback warning not found in: {log_cm.output}",
            )


class TestGetAllKosyncDocumentsUserFilter(unittest.TestCase):
    """T2-b: get_all_kosync_documents must filter by user_id when given."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ['DATA_DIR'] = self.tmp
        os.environ['BOOKS_DIR'] = self.tmp

        self.svc = DatabaseService(os.path.join(self.tmp, "kosync-docs.db"))
        admin = self.svc.create_user("admin", "secret", role="admin")
        self.alice = self.svc.create_user("alice", "pw", role="user")

        # KosyncDocuments for different users
        self.svc.save_kosync_document(KosyncDocument(
            document_hash="doc-admin-1",
            device="test",
            user_id=admin.id,
        ))
        self.svc.save_kosync_document(KosyncDocument(
            document_hash="doc-alice-1",
            device="test",
            user_id=self.alice.id,
        ))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_get_all_kosync_documents_filters_by_user_id(self):
        """With user_id, only that user's documents are returned."""
        docs = self.svc.get_all_kosync_documents(user_id=self.alice.id)
        hashes = [d.document_hash for d in docs]
        self.assertIn("doc-alice-1", hashes)
        self.assertNotIn("doc-admin-1", hashes)

    def test_get_all_kosync_documents_no_filter_returns_all(self):
        """Without user_id, all documents are returned."""
        docs = self.svc.get_all_kosync_documents()
        hashes = [d.document_hash for d in docs]
        self.assertIn("doc-alice-1", hashes)
        self.assertIn("doc-admin-1", hashes)


if __name__ == "__main__":
    unittest.main()
