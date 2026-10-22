"""A service the admin switched off install-wide stays off for every user.

Before this, ``resolve_setting`` took the per-user value first and only consulted
the global when the user's was blank — so a per-user ``true`` overrode a global
``false``. The reported symptom: Storyteller was off in Settings, both users had it
on in their own integrations, and the poller kept calling a Storyteller that was not
even running, every 60 seconds.

The global is now authoritative for service gates, and only ever takes capability
away. What each user chose is left in the database untouched, so switching the
global back on restores their choice rather than waking everyone up disabled.
"""

import os
import re
import unittest

from src.utils.user_config import (
    SERVICE_ENABLE_KEYS,
    global_service_disabled,
    resolve_setting,
)


class _EnvCase(unittest.TestCase):
    KEYS = ('STORYTELLER_ENABLED', 'ABS_ENABLED', 'READEST_ANNOTATION_SYNC',
            'BOOKLORE_ANNOTATION_SYNC', 'ABS_KEY')

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in self.KEYS}
        self.addCleanup(self._restore)

    def _restore(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class TestGlobalServiceGate(_EnvCase):
    def test_global_off_beats_a_user_who_turned_it_on(self):
        """The reported case, exactly."""
        os.environ['STORYTELLER_ENABLED'] = 'false'
        self.assertEqual(
            resolve_setting({'STORYTELLER_ENABLED': 'true'}, 'STORYTELLER_ENABLED', ''),
            'false',
        )

    def test_every_falsey_spelling_of_the_global_counts(self):
        for value in ('false', 'off', '0', 'no', 'FALSE'):
            with self.subTest(value=value):
                os.environ['ABS_ENABLED'] = value
                self.assertEqual(resolve_setting({'ABS_ENABLED': 'true'}, 'ABS_ENABLED', ''), 'false')

    def test_global_on_leaves_the_user_in_charge(self):
        os.environ['ABS_ENABLED'] = 'true'
        self.assertEqual(resolve_setting({'ABS_ENABLED': 'false'}, 'ABS_ENABLED', ''), 'false')
        self.assertEqual(resolve_setting({'ABS_ENABLED': 'true'}, 'ABS_ENABLED', ''), 'true')
        self.assertEqual(resolve_setting({}, 'ABS_ENABLED', ''), 'true')

    def test_an_unset_global_is_not_a_decision(self):
        """Several gates ship unseeded; absence must not read as 'off'."""
        os.environ.pop('ABS_ENABLED', None)
        self.assertFalse(global_service_disabled('ABS_ENABLED'))
        self.assertEqual(resolve_setting({'ABS_ENABLED': 'true'}, 'ABS_ENABLED', ''), 'true')

    def test_feature_sub_toggles_are_not_gated(self):
        """`*_ANNOTATION_SYNC` defaults to 'false' globally while users legitimately
        turn it on for themselves — enforcing the global there would switch off work
        people already rely on."""
        for key in ('READEST_ANNOTATION_SYNC', 'BOOKLORE_ANNOTATION_SYNC'):
            with self.subTest(key=key):
                self.assertNotIn(key, SERVICE_ENABLE_KEYS)
                os.environ[key] = 'false'
                self.assertEqual(resolve_setting({key: 'true'}, key, ''), 'true')

    def test_credentials_are_untouched_for_non_gate_keys(self):
        os.environ['ABS_KEY'] = 'global-token'
        self.assertEqual(resolve_setting({'ABS_KEY': 'user-token'}, 'ABS_KEY', ''), 'user-token')


class TestGatedClients(_EnvCase):
    """The gate has to bite at the client, which is what the poller consults."""

    def test_storyteller_client_is_not_configured_when_globally_off(self):
        from src.api.storyteller_api import StorytellerAPIClient

        os.environ['STORYTELLER_ENABLED'] = 'false'
        creds = {
            'STORYTELLER_ENABLED': 'true',
            'STORYTELLER_USER': 'reader',
            'STORYTELLER_PASSWORD': 'secret',
        }
        os.environ['STORYTELLER_API_URL'] = 'http://storyteller.test'
        self.addCleanup(os.environ.pop, 'STORYTELLER_API_URL', None)
        self.assertFalse(StorytellerAPIClient(credentials=creds).is_configured())

        os.environ['STORYTELLER_ENABLED'] = 'true'
        self.assertTrue(StorytellerAPIClient(credentials=creds).is_configured())

    def test_abs_client_is_not_configured_when_globally_off(self):
        from src.api.api_clients import ABSClient

        saved = {k: os.environ.get(k) for k in ('ABS_SERVER',)}
        os.environ['ABS_SERVER'] = 'http://abs.test'
        self.addCleanup(lambda: (os.environ.__setitem__('ABS_SERVER', saved['ABS_SERVER'])
                                 if saved['ABS_SERVER'] is not None
                                 else os.environ.pop('ABS_SERVER', None)))
        creds = {'ABS_ENABLED': 'true', 'ABS_KEY': 'user-token'}

        os.environ['ABS_ENABLED'] = 'false'
        self.assertFalse(ABSClient(credentials=creds).is_configured())
        os.environ['ABS_ENABLED'] = 'true'
        self.assertTrue(ABSClient(credentials=creds).is_configured())


if __name__ == '__main__':
    unittest.main()


class TestIntegrationPageRendering(unittest.TestCase):
    """The page has to say why the switch will not move, and must not quietly
    erase what the user chose while it is disabled."""

    TEMPLATE = 'account_integrations.html'

    def _render(self, *, creds, master):
        from pathlib import Path
        from jinja2 import Environment, FileSystemLoader

        templates = Path(__file__).resolve().parent.parent / 'templates'
        env = Environment(loader=FileSystemLoader(str(templates)))
        # The page wraps its text in _() / {% trans %}; render it untranslated,
        # with newstyle gettext as Flask-Babel uses.
        env.add_extension('jinja2.ext.i18n')
        env.install_null_translations(newstyle=True)
        env.globals['url_for'] = lambda endpoint, **kw: f'/{endpoint}'
        # The page extends base.html; render only the group markup by pulling the
        # body block out of the child template.
        source = (templates / self.TEMPLATE).read_text(encoding='utf-8')
        start = source.index("{% block content %}")
        end = source.index("{% endblock %}", start)
        body = source[start + len("{% block content %}"):end]
        return env.from_string(body).render(
            groups=[('Storyteller', [
                ('STORYTELLER_ENABLED', 'Enabled', 'bool'),
                ('STORYTELLER_USER', 'Username', 'text'),
            ])],
            creds=creds,
            master=master,
            service_enable_keys=SERVICE_ENABLE_KEYS,
            allow_master_fallback=False,
            message=None,
            account_user=type('U', (), {'username': 'reader', 'role': 'user'})(),
            user_test_services={},
        )

    def test_globally_off_disables_the_toggle_and_says_so(self):
        html = self._render(
            creds={'STORYTELLER_ENABLED': 'true'},
            master={'STORYTELLER_ENABLED': 'false'},
        )
        gate = re.search(r'<input type="checkbox" id="integration_gate_1".*?>', html, re.S)
        self.assertIsNotNone(gate)
        self.assertIn('disabled', gate.group(0))
        self.assertNotIn('checked', gate.group(0))
        self.assertIn('Off server-wide', html)
        self.assertIn('Turned off for everyone', html)

    def test_the_users_own_choice_is_preserved_while_disabled(self):
        """A disabled checkbox posts nothing and the save loop reads an absent bool
        as 'false'; the hidden field is what keeps their 'on' from being erased."""
        html = self._render(
            creds={'STORYTELLER_ENABLED': 'true'},
            master={'STORYTELLER_ENABLED': 'false'},
        )
        self.assertIn('<input type="hidden" name="STORYTELLER_ENABLED" value="on">', html)

    def test_no_hidden_field_when_the_user_had_it_off(self):
        html = self._render(
            creds={'STORYTELLER_ENABLED': 'false'},
            master={'STORYTELLER_ENABLED': 'false'},
        )
        self.assertNotIn('<input type="hidden" name="STORYTELLER_ENABLED"', html)

    def test_globally_on_leaves_the_toggle_usable(self):
        html = self._render(
            creds={'STORYTELLER_ENABLED': 'true'},
            master={'STORYTELLER_ENABLED': 'true'},
        )
        self.assertNotIn('Off server-wide', html)
        self.assertNotIn('Turned off for everyone', html)
        self.assertIn('checked', html)


class TestAdminIntegrationPageRendering(TestIntegrationPageRendering):
    """The admin-managed copy of the page carries the same rules."""

    TEMPLATE = 'admin_user_integrations.html'

    def _render(self, *, creds, master):
        from pathlib import Path
        from jinja2 import Environment, FileSystemLoader

        templates = Path(__file__).resolve().parent.parent / 'templates'
        env = Environment(loader=FileSystemLoader(str(templates)))
        # The page wraps its text in _() / {% trans %}; render it untranslated,
        # with newstyle gettext as Flask-Babel uses.
        env.add_extension('jinja2.ext.i18n')
        env.install_null_translations(newstyle=True)
        env.globals['url_for'] = lambda endpoint, **kw: f'/{endpoint}'
        source = (templates / self.TEMPLATE).read_text(encoding='utf-8')
        start = source.index("{% block content %}")
        end = source.index("{% endblock %}", start)
        body = source[start + len("{% block content %}"):end]
        return env.from_string(body).render(
            groups=[('Storyteller', [
                ('STORYTELLER_ENABLED', 'Enabled', 'bool'),
                ('STORYTELLER_USER', 'Username', 'text'),
            ])],
            creds=creds,
            master=master,
            service_enable_keys=SERVICE_ENABLE_KEYS,
            allow_master_fallback=False,
            message=None,
            target_user=type('U', (), {'id': 2, 'username': 'reader', 'role': 'user'})(),
            user_test_services={},
        )

    def test_globally_off_disables_the_toggle_and_says_so(self):
        html = self._render(
            creds={'STORYTELLER_ENABLED': 'true'},
            master={'STORYTELLER_ENABLED': 'false'},
        )
        gate = re.search(r'<input type="checkbox" id="integration_gate_1".*?>', html, re.S)
        self.assertIsNotNone(gate)
        self.assertIn('disabled', gate.group(0))
        self.assertNotIn('checked', gate.group(0))
        self.assertIn('Off server-wide', html)
        self.assertIn('Turned off for everyone', html)


class TestReadestServiceGate(_EnvCase):
    """Readest had no global switch at all — its highlight relay and both uploads
    were per-user only, so there was nothing for an admin to turn off. `READEST_ENABLED`
    is that switch, and it covers every Readest feature at once."""

    KEYS = _EnvCase.KEYS + ('READEST_ENABLED', 'READEST_UPLOAD_READING', 'READEST_UPLOAD_ON_MATCH')

    def test_registered_and_defaults_on(self):
        from src.utils.config_loader import ALL_SETTINGS, DEFAULT_CONFIG

        self.assertIn('READEST_ENABLED', ALL_SETTINGS)
        # Readest predates its gate, so 'true' keeps every existing install working
        # when bootstrap reconciles the key in.
        self.assertEqual(DEFAULT_CONFIG['READEST_ENABLED'], 'true')

    def test_declared_per_user_and_gated(self):
        from src.utils.user_config import PER_USER_CREDENTIAL_KEYS, PER_USER_FIELD_GROUPS

        self.assertIn('READEST_ENABLED', SERVICE_ENABLE_KEYS)
        self.assertIn('READEST_ENABLED', PER_USER_CREDENTIAL_KEYS)
        self.assertIn(('READEST_ENABLED', 'Enabled', 'bool'), dict(PER_USER_FIELD_GROUPS)['Readest'])

    def test_cwa_kobo_sync_is_gated_too(self):
        """CWA_SYNC_ENABLED has a real global toggle in Settings, so unlike the
        annotation flags it can be enforced."""
        self.assertIn('CWA_SYNC_ENABLED', SERVICE_ENABLE_KEYS)
        os.environ['CWA_SYNC_ENABLED'] = 'false'
        self.addCleanup(os.environ.pop, 'CWA_SYNC_ENABLED', None)
        self.assertEqual(resolve_setting({'CWA_SYNC_ENABLED': 'true'}, 'CWA_SYNC_ENABLED', ''), 'false')

    def test_annotation_cycle_skips_every_readest_feature_when_off(self):
        from unittest.mock import MagicMock, patch
        from src.services.annotation_sync_service import AnnotationSyncService

        creds = {
            'READEST_ENABLED': 'true',
            'READEST_ANNOTATION_SYNC': 'true',
            'READEST_UPLOAD_READING': 'true',
        }
        db = MagicMock()
        service = AnnotationSyncService(db)
        service._enumerate_users = lambda: [(1, dict(creds))]
        service._readest_sync = MagicMock()
        service._readest_sync.sync_user.return_value = False

        os.environ['READEST_ENABLED'] = 'false'
        with patch('src.services.readest_upload_service.ReadestUploadService') as upload:
            service.run_cycle()
        service._readest_sync.sync_user.assert_not_called()
        upload.assert_not_called()

        os.environ['READEST_ENABLED'] = 'true'
        with patch('src.services.readest_upload_service.ReadestUploadService') as upload:
            upload.return_value.publish_reading_books.return_value = {'uploaded': 0}
            service.run_cycle()
        service._readest_sync.sync_user.assert_called_once()

    def test_upload_on_match_respects_the_service_gate(self):
        from unittest.mock import MagicMock, patch
        import src.web_server as ws

        book = MagicMock(original_ebook_filename='Some Book.epub', ebook_filename='Some Book.epub')
        values = {'READEST_ENABLED': 'false', 'READEST_UPLOAD_ON_MATCH': 'true'}
        with patch.object(ws, 'user_setting', lambda key, default='': values.get(key, default)):
            with patch.object(ws, '_spawn_user_background') as spawn:
                ws._publish_saved_ebook_to_readest(book)
        spawn.assert_not_called()
