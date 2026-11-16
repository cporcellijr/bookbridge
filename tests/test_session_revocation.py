"""Dashboard sessions die when the user's password changes."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from src.db.database_service import DatabaseService
from tests.test_webserver import MockContainer

_TEMPLATES = str(Path(__file__).parent.parent / "templates")


class TestSessionRevocation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._saved_env = {
            k: os.environ.get(k)
            for k in ('DATA_DIR', 'BOOKS_DIR', 'TEMPLATE_DIR', 'REMOTE_AUTH_ENABLED', 'REMOTE_AUTH_TRUSTED_PROXIES')
        }
        os.environ.update(DATA_DIR=self.tmp, BOOKS_DIR=self.tmp, TEMPLATE_DIR=_TEMPLATES)
        os.environ.pop('REMOTE_AUTH_ENABLED', None)
        self.svc = DatabaseService(os.path.join(self.tmp, "rev.db"))
        self.admin = self.svc.create_user("admin", "secret", role="admin")
        self.reader = self.svc.create_user("reader", "pw", role="user")
        container = MockContainer()
        container.mock_database_service = self.svc

        import src.db.migration_utils
        self._orig_init = src.db.migration_utils.initialize_database
        src.db.migration_utils.initialize_database = lambda data_dir: self.svc

        from src.web_server import create_app
        self.app, _ = create_app(test_container=container)
        self.app.config['TESTING'] = True
        self.app.config['LOGIN_DISABLED'] = False

    def tearDown(self):
        import src.db.migration_utils
        src.db.migration_utils.initialize_database = self._orig_init
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _login(self, username='reader', password='pw'):
        client = self.app.test_client()
        client.post('/login', data={'username': username, 'password': password})
        return client

    def _status(self, client):
        return client.get('/account', follow_redirects=False).status_code

    def test_session_from_before_a_password_change_is_rejected(self):
        old_session = self._login()
        self.assertEqual(self._status(old_session), 200)
        self.svc.set_user_password(self.reader.id, 'new-pw-123')
        self.assertEqual(self._status(old_session), 302)

    def test_admin_reset_does_not_keep_target_session_alive(self):
        reader_session = self._login()
        admin_session = self._login('admin', 'secret')
        admin_session.post('/admin/users', data={
            'action': 'reset_password', 'user_id': str(self.reader.id), 'password': 'reset-pw-1',
        })
        self.assertEqual(self._status(reader_session), 302)
        self.assertEqual(self._status(admin_session), 200)

    def test_user_who_changes_own_password_stays_logged_in_and_others_do_not(self):
        changer = self._login()
        other_device = self._login()
        resp = changer.post('/account', data={
            'current_password': 'pw', 'new_password': 'np123456', 'confirm_password': 'np123456',
        })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._status(changer), 200)
        self.assertEqual(self._status(other_device), 302)

    def test_session_without_marker_is_rejected(self):
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess['user_id'] = self.reader.id
        self.assertEqual(self._status(client), 302)

    def test_remote_auth_session_is_stamped_and_keeps_working(self):
        os.environ['REMOTE_AUTH_ENABLED'] = 'true'
        os.environ['REMOTE_AUTH_TRUSTED_PROXIES'] = '127.0.0.1'
        client = self.app.test_client()
        headers = {'Remote-User': 'reader'}
        self.assertEqual(client.get('/account', headers=headers).status_code, 200)
        self.assertEqual(client.get('/account').status_code, 200)
        with client.session_transaction() as sess:
            self.assertTrue(sess.get('session_marker'))


if __name__ == '__main__':
    unittest.main()
