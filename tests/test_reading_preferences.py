"""Reading preferences: the admin's Settings value is the default, each reader can override it.

These settings describe how one reader wants their own books handled (their devices,
their stats, their Library view). They used to be server-wide only, so one household
member turning highlight sync off turned it off for everyone.
"""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from src.utils.user_config import (
    _ALLOW_GLOBAL_FALLBACK_KEY,
    PER_USER_CREDENTIAL_KEYS,
    PER_USER_FIELD_GROUPS,
    PER_USER_PREFERENCE_KEYS,
    user_preference,
    user_preference_enabled,
)
from src.utils.user_context import (
    reset_current_user_credentials,
    reset_current_user_id,
    set_current_user_credentials,
    set_current_user_id,
)

KEY = 'KOREADER_ANNOTATION_SYNC'


class _EnvCase(unittest.TestCase):
    def setUp(self):
        self._saved_env = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved_env)


class TestUserPreference(_EnvCase):
    def test_without_a_user_the_server_value_applies(self):
        os.environ[KEY] = 'false'
        self.assertFalse(user_preference_enabled(KEY, 'true'))

    def test_a_readers_own_choice_overrides_the_server_value(self):
        os.environ[KEY] = 'true'
        token = set_current_user_credentials({KEY: 'false', _ALLOW_GLOBAL_FALLBACK_KEY: False})
        try:
            self.assertFalse(user_preference_enabled(KEY, 'true'))
        finally:
            reset_current_user_credentials(token)

    def test_a_blank_choice_follows_the_server_value_even_for_a_regular_user(self):
        """Unlike an account credential, a regular user does fall back to the global value."""
        os.environ[KEY] = 'false'
        token = set_current_user_credentials({KEY: '', _ALLOW_GLOBAL_FALLBACK_KEY: False})
        try:
            self.assertFalse(user_preference_enabled(KEY, 'true'))
            os.environ[KEY] = 'true'
            self.assertTrue(user_preference_enabled(KEY, 'false'))
        finally:
            reset_current_user_credentials(token)

    def test_a_kosync_request_reads_the_choice_by_user_id(self):
        os.environ[KEY] = 'true'
        db = Mock()
        db.get_user_credential.return_value = 'false'
        token = set_current_user_id(7)
        try:
            self.assertFalse(user_preference_enabled(KEY, 'true', db))
        finally:
            reset_current_user_id(token)
        db.get_user_credential.assert_called_once_with(7, KEY)

    def test_a_failed_lookup_falls_back_to_the_server_value(self):
        os.environ[KEY] = 'true'
        db = Mock()
        db.get_user_credential.side_effect = RuntimeError('database is locked')
        token = set_current_user_id(7)
        try:
            with self.assertLogs('src.utils.user_config', level='WARNING'):
                self.assertTrue(user_preference_enabled(KEY, 'false', db))
        finally:
            reset_current_user_id(token)

    def test_unset_everywhere_uses_the_callers_default(self):
        os.environ.pop(KEY, None)
        self.assertEqual(user_preference(KEY, 'true'), 'true')


class TestPreferenceRegistry(unittest.TestCase):
    def test_every_preference_is_on_the_reading_preferences_card(self):
        groups = dict(PER_USER_FIELD_GROUPS)
        fields = {key: ftype for key, _label, ftype in groups['Reading preferences']}
        self.assertEqual(set(fields), set(PER_USER_PREFERENCE_KEYS))
        self.assertEqual(set(fields.values()), {'pref'})

    def test_the_admin_api_accepts_only_on_off_or_server_default(self):
        from src.api.admin_api import _validate_field
        self.assertEqual(_validate_field(KEY, 'pref', ''), '')
        self.assertEqual(_validate_field(KEY, 'pref', 'false'), 'false')
        self.assertEqual(_validate_field(KEY, 'pref', True), 'true')
        with self.assertRaises(ValueError):
            _validate_field(KEY, 'pref', 'maybe')

    def test_preferences_are_not_account_credentials(self):
        """A credential key never falls back for a regular user; a preference must."""
        self.assertEqual(PER_USER_PREFERENCE_KEYS & PER_USER_CREDENTIAL_KEYS, frozenset())


class TestConsumersHonourTheReader(_EnvCase):
    def test_koreader_highlight_sync_follows_the_reader(self):
        import src.api.kosync_server as kosync_server
        os.environ[KEY] = 'true'
        db = Mock()
        db.get_user_credential.return_value = 'false'
        saved = kosync_server._database_service
        kosync_server._database_service = db
        token = set_current_user_id(3)
        try:
            self.assertFalse(kosync_server._annotation_sync_enabled())
        finally:
            reset_current_user_id(token)
            kosync_server._database_service = saved

    def test_completion_propagation_follows_the_reader(self):
        from src.sync_manager import SyncManager
        os.environ['SYNC_COMPLETION_PROPAGATION'] = 'false'
        token = set_current_user_credentials({'SYNC_COMPLETION_PROPAGATION': 'true'})
        try:
            self.assertTrue(SyncManager._completion_propagation_enabled(Mock()))
        finally:
            reset_current_user_credentials(token)
        self.assertFalse(SyncManager._completion_propagation_enabled(Mock()))

    def test_sync_cycles_carry_the_readers_choices(self):
        """sync_cycle installs the registry bundle's credentials as the ambient ones;
        that bundle used to keep only account keys, so every saved preference was
        dropped and the server default applied during sync."""
        from src.services.user_client_registry import UserClientRegistry
        from src.sync_manager import SyncManager
        os.environ.update({'SYNC_COMPLETION_PROPAGATION': 'false', 'STORYTELLER_LISTENING_SESSIONS': 'true',
                           'KOREADER_STATUS_SYNC_ENABLED': 'true'})
        db = Mock()
        db.get_user_credentials.return_value = {
            'SYNC_COMPLETION_PROPAGATION': 'true', 'STORYTELLER_LISTENING_SESSIONS': 'false',
            'KOREADER_STATUS_SYNC_ENABLED': 'false', 'NOT_A_SETTING': 'x',
        }
        db.get_user.return_value = Mock(role='user')
        credentials = UserClientRegistry(db, Mock(), Mock()).get_clients(7).credentials
        self.assertNotIn('NOT_A_SETTING', credentials)
        token = set_current_user_credentials(credentials)
        try:
            self.assertTrue(SyncManager._completion_propagation_enabled(Mock()))
            self.assertFalse(user_preference_enabled('STORYTELLER_LISTENING_SESSIONS', 'true'))
            self.assertFalse(user_preference_enabled('KOREADER_STATUS_SYNC_ENABLED', 'true', db))
        finally:
            reset_current_user_credentials(token)


class TestReadingPreferencesPage(_EnvCase):
    """A regular reader picks On/Off/Server default on My Integrations."""

    def setUp(self):
        super().setUp()
        from tests.test_webserver import MockContainer
        from src.db.database_service import DatabaseService

        self.tmp = tempfile.mkdtemp()
        os.environ.update(DATA_DIR=self.tmp, BOOKS_DIR=self.tmp,
                          TEMPLATE_DIR=str(Path(__file__).parent.parent / 'templates'))
        self.svc = DatabaseService(os.path.join(self.tmp, 'prefs.db'))
        self.svc.create_user('admin', 'secret', role='admin')
        self.reader = self.svc.create_user('reader', 'pw', role='user')
        container = MockContainer()
        container.mock_database_service = self.svc

        import src.db.migration_utils
        self._orig_init = src.db.migration_utils.initialize_database
        src.db.migration_utils.initialize_database = lambda _d: self.svc
        from src.web_server import create_app
        self.app, _ = create_app(test_container=container)
        self.app.config.update(TESTING=True, LOGIN_DISABLED=False)
        self.client = self.app.test_client()
        self.client.post('/login', data={'username': 'reader', 'password': 'pw'})

    def tearDown(self):
        import src.db.migration_utils
        src.db.migration_utils.initialize_database = self._orig_init
        shutil.rmtree(self.tmp, ignore_errors=True)
        super().tearDown()

    def test_the_page_offers_server_default_on_and_off(self):
        os.environ[KEY] = 'true'
        html = self.client.get('/account/integrations').get_data(as_text=True)
        select = html.split(f'<select id="{KEY}" name="{KEY}">', 1)[1].split('</select>', 1)[0]
        self.assertIn('<option value="" selected>Server default (on)</option>', select)
        self.assertIn('<option value="false" >Off</option>', select)

    def test_off_is_saved_for_this_reader_only_and_blank_goes_back_to_the_default(self):
        os.environ[KEY] = 'true'
        self.client.post('/account/integrations', data={KEY: 'false'})
        self.assertEqual(self.svc.get_user_credential(self.reader.id, KEY), 'false')
        self.assertEqual(os.environ[KEY], 'true')

        self.client.post('/account/integrations', data={KEY: ''})
        self.assertEqual(self.svc.get_user_credential(self.reader.id, KEY) or '', '')


if __name__ == '__main__':
    unittest.main()
