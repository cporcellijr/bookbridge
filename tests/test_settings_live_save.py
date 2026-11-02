"""Saving settings restarts the app only when a startup-captured setting changed.

Every save used to restart BookBridge, even a single highlight-sync checkbox, which
users read as "some settings restart the container". Most settings are read per
call, so they apply the moment they are saved; only RESTART_REQUIRED_SETTINGS are
captured at startup and still need the restart.
"""
import ast
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.utils.config_loader import KNOWN_SETTING_KEYS, RESTART_REQUIRED_SETTINGS

ROOT = Path(__file__).parent.parent


class _Container:
    def __init__(self, db):
        self._db = db

    def database_service(self): return self._db
    def sync_manager(self): return Mock()
    def booklore_client(self): return Mock()
    def sync_clients(self): return {}
    def data_dir(self): return Path(tempfile.gettempdir())
    def books_dir(self): return Path(tempfile.gettempdir())
    def epub_cache_dir(self): return Path(tempfile.gettempdir()) / 'test_epub_cache'

    def __getattr__(self, _name):
        return Mock


class TestSettingsSaveRestartsOnlyWhenNeeded(unittest.TestCase):
    def setUp(self):
        self._saved_env = dict(os.environ)
        self.temp_dir = tempfile.mkdtemp()
        os.environ['DATA_DIR'] = self.temp_dir
        os.environ['TEMPLATE_DIR'] = str(ROOT / 'templates')
        os.environ['STATIC_DIR'] = str(ROOT / 'static')
        self.store = {'SYNC_PERIOD_MINS': '5', 'DATA_DIR': self.temp_dir}
        db = Mock()
        db.get_all_settings.side_effect = lambda: dict(self.store)
        db.set_setting.side_effect = self.store.__setitem__

        import src.db.migration_utils
        self._orig_init_db = src.db.migration_utils.initialize_database
        src.db.migration_utils.initialize_database = lambda _data_dir: db

        from src.web_server import create_app
        self.app, _ = create_app(test_container=_Container(db))
        self.app.config['TESTING'] = True
        self.client = self.app.test_client()

    def tearDown(self):
        import shutil
        import src.db.migration_utils
        src.db.migration_utils.initialize_database = self._orig_init_db
        os.environ.clear()
        os.environ.update(self._saved_env)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _post(self, **form):
        """POST the form as the page sends it: the two switches bootstrap seeds on
        stay checked unless a test passes None to uncheck one."""
        data = {'SYNC_PERIOD_MINS': '5', 'DATA_DIR': self.temp_dir,
                'ABS_ENABLED': 'on', 'INSTANT_SYNC_ENABLED': 'on', **form}
        return self.client.post('/settings', data={k: v for k, v in data.items() if v is not None})

    def test_a_live_setting_saves_and_applies_without_a_restart(self):
        response = self._post(KOREADER_ANNOTATION_SYNC='on', SUGGESTIONS_AUTO_SCAN_MINUTES='30')

        self.assertEqual(response.status_code, 302)
        self.assertNotIn(b'Restarting the application', response.data)
        self.assertEqual(self.store['KOREADER_ANNOTATION_SYNC'], 'true')
        self.assertEqual(os.environ['KOREADER_ANNOTATION_SYNC'], 'true')
        self.assertEqual(os.environ['SUGGESTIONS_AUTO_SCAN_MINUTES'], '30')
        with self.client.session_transaction() as session:
            self.assertIn('already in effect', session['message'])

    def test_changing_a_startup_setting_still_restarts(self):
        response = self._post(SYNC_PERIOD_MINS='10')

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Restarting the application', response.data)
        self.assertEqual(self.store['SYNC_PERIOD_MINS'], '10')

    def test_a_startup_checkbox_flip_restarts_but_a_respelled_one_does_not(self):
        self.store['INSTANT_SYNC_ENABLED'] = 'True'
        self.assertEqual(self._post().status_code, 302)
        self.assertEqual(self.store['INSTANT_SYNC_ENABLED'], 'true')

        response = self._post(INSTANT_SYNC_ENABLED=None)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Restarting the application', response.data)

    def test_returns_to_the_tab_the_save_came_from(self):
        self.assertTrue(self._post(return_tab='reading').headers['Location'].endswith('/settings#reading'))
        self.assertTrue(self._post(return_tab='x"><script>').headers['Location'].endswith('/settings'))

    def test_one_control_saves_every_apps_setting_including_switched_off_ones(self):
        """Polling, shelf watch, highlight interval and tracker cooldown are one choice
        each; saving copies it into every app's own setting."""
        from src.utils.config_loader import MERGED_SETTINGS
        self._post(ALL_APPS_POLL_MODE='custom', ALL_APPS_POLL_SECONDS='60',
                   ALL_APPS_POLL_WAIT_FOR_SETTLE=['off', 'on'],
                   ALL_APPS_SHELF_WATCH_ENABLED='off', ALL_APPS_SHELF_WATCH_NAME='Next Up',
                   ALL_APPS_ANNOTATION_SYNC_MINUTES='20', ALL_APPS_TRACKER_COOLDOWN_MINS='90')
        expected = {'ALL_APPS_POLL_MODE': 'custom', 'ALL_APPS_POLL_SECONDS': '60',
                    'ALL_APPS_POLL_WAIT_FOR_SETTLE': 'true', 'ALL_APPS_SHELF_WATCH_ENABLED': 'false',
                    'ALL_APPS_SHELF_WATCH_NAME': 'Next Up', 'ALL_APPS_ANNOTATION_SYNC_MINUTES': '20',
                    'ALL_APPS_TRACKER_COOLDOWN_MINS': '90'}
        for merged, value in expected.items():
            for target in MERGED_SETTINGS[merged]:
                self.assertEqual(self.store.get(target), value, target)
            self.assertNotIn(merged, self.store)
        self.assertEqual(self.store['KAVITA_POLL_WAIT_FOR_SETTLE'], 'true')

    def test_the_aligner_model_and_device_restart(self):
        """The forced aligner keeps the model and device it loaded first."""
        for key, value in (('CTC_DEVICE', 'cuda'), ('CTC_QUARTZNET_MODEL_PATH', '/data/models/other')):
            self.assertIn(b'Restarting the application', self._post(**{key: value}).data, key)

    def test_an_untouched_merged_control_keeps_each_apps_own_value(self):
        """An unchanged Save used to copy BookOrbit's shelf watch and poll timer into
        every app, switching on Grimmory's and Kavita's watches behind the admin's back."""
        from src.utils.config_loader import MERGED_SETTINGS, merged_setting_values
        mixed = {'BOOKORBIT_SHELF_WATCH_ENABLED': 'true', 'BOOKORBIT_SHELF_WATCH_NAME': 'Next audio',
                 'BOOKLORE_SHELF_WATCH_ENABLED': 'false', 'BOOKLORE_SHELF_WATCH_NAME': 'Manual only',
                 'KAVITA_SHELF_WATCH_ENABLED': 'false',
                 'BOOKORBIT_POLL_MODE': 'custom', 'BOOKORBIT_POLL_SECONDS': '60',
                 'CWA_SYNC_POLL_MODE': 'global', 'CWA_SYNC_POLL_SECONDS': '300'}
        os.environ.update(mixed)
        self.store.update(mixed)
        shown = merged_setting_values(os.environ)
        page = {k: ['off', 'on'] if v == 'true' else ('off' if v == 'false' else v)
                for k, v in shown.items() if k in MERGED_SETTINGS}
        self.assertEqual(self._post(**page).status_code, 302)
        self.assertEqual(self.store['BOOKORBIT_SHELF_WATCH_ENABLED'], 'true')
        self.assertEqual(self.store['BOOKLORE_SHELF_WATCH_ENABLED'], 'false')
        self.assertEqual(self.store['KAVITA_SHELF_WATCH_ENABLED'], 'false')
        self.assertEqual(self.store['BOOKLORE_SHELF_WATCH_NAME'], 'Manual only')
        self.assertEqual(self.store['CWA_SYNC_POLL_MODE'], 'global')
        self.assertEqual(self.store['CWA_SYNC_POLL_SECONDS'], '300')

        self._post(**{**page, 'ALL_APPS_SHELF_WATCH_NAME': 'Queue'})
        self.assertEqual(self.store['BOOKLORE_SHELF_WATCH_NAME'], 'Queue')
        self.assertEqual(self.store['BOOKLORE_SHELF_WATCH_ENABLED'], 'false')

    def test_the_page_says_when_apps_differ(self):
        self.assertNotIn('merged-mixed-note">', self.client.get('/settings').get_data(as_text=True))
        os.environ.update({'BOOKORBIT_POLL_MODE': 'custom', 'CWA_SYNC_POLL_MODE': 'global'})
        self.assertIn('merged-mixed-note">', self.client.get('/settings').get_data(as_text=True))

    def test_an_unticked_merged_switch_turns_every_app_off(self):
        self._post(ALL_APPS_POLL_WAIT_FOR_SETTLE=['off', 'on'])
        self._post(ALL_APPS_POLL_WAIT_FOR_SETTLE='off')
        self.assertEqual(self.store['STORYTELLER_POLL_WAIT_FOR_SETTLE'], 'false')
        self.assertEqual(self.store['BOOKORBIT_AUDIO_POLL_WAIT_FOR_SETTLE'], 'false')

    def test_a_card_switched_off_folds_its_settings_away(self):
        os.environ.pop('REMOTE_AUTH_ENABLED', None)
        html = self.client.get('/settings').get_data(as_text=True)
        self.assertIn('id="remote_auth_body" class="section-body collapsed"', html)
        for card in html.split('<div class="settings-section')[1:]:
            header, _, body = card.partition('class="section-body')
            if 'toggle-switch' in header and 'toggleSection(' not in header:
                self.assertNotRegex(body, r'<(input|select|textarea)\b', header.split('</h3>')[0])

    def test_find_shelves_looks_in_every_library_app_in_use(self):
        def app(configured, shelves):
            return Mock(is_configured=Mock(return_value=configured), get_all_shelves=Mock(return_value=shelves))
        bundle = Mock(booklore_client=app(False, [{'name': 'Grimmory only'}]),
                      bookorbit_client=app(True, [{'id': 3, 'name': 'Up Next'}, {'id': 4, 'name': 'Favourites'}]),
                      kavita_client=app(True, [{'title': 'up next'}]))
        with patch('src.web_server.uc', return_value=bundle):
            shelves = self.client.get('/api/watch-shelves').get_json()
        self.assertEqual(shelves, [{'id': 'Favourites', 'name': 'Favourites (BookOrbit)'},
                                   {'id': 'Up Next', 'name': 'Up Next (BookOrbit, Kavita)'}])
        bundle.booklore_client.get_all_shelves.assert_not_called()

    def test_the_page_marks_restart_settings_and_ships_defaults(self):
        html = self.client.get('/settings').get_data(as_text=True)
        self.assertIn('"SYNC_PERIOD_MINS"', html.split('RESTART_KEYS', 1)[1].split(';', 1)[0])
        self.assertIn('"KOSYNC_PUT_DEBOUNCE_SECONDS": "300"', html.split('SETTING_DEFAULTS', 1)[1].split(';', 1)[0])


# Classes built fresh for each use, so an __init__ read is still a per-call read.
_PER_CALL_CLASSES = {'LocalWhisperProvider', 'DeepgramProvider', 'WhisperCppServerProvider'}
# Functions that only run while the app starts.
_STARTUP_FUNCTIONS = {'setup_dependencies', 'setup_telegram_logging'}
_READERS = {'getenv', 'env_truthy', '_get_float_env', 'user_setting'}


def _startup_reads():
    """(key, location) for every registered setting read while the app starts."""
    found = []
    for path in (ROOT / 'src').rglob('*.py'):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        rel = path.relative_to(ROOT).as_posix()

        def walk(node, classes, funcs):
            if isinstance(node, ast.ClassDef):
                classes = classes + [node.name]
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                funcs = funcs + [node.name]
            elif isinstance(node, ast.Lambda):
                funcs = funcs + ['<lambda>']
            key = None
            if isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, 'id', None)
                is_environ_get = name == 'get' and ast.unparse(func.value) == 'os.environ'
                if is_environ_get or name in _READERS:
                    key = node.args[0].value
            elif (isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load)
                  and ast.unparse(node.value) == 'os.environ' and isinstance(node.slice, ast.Constant)):
                key = node.slice.value
            if key in KNOWN_SETTING_KEYS:
                at_startup = (
                    not funcs
                    or funcs[-1] in _STARTUP_FUNCTIONS
                    or (funcs[-1] == '__init__' and classes and classes[-1] not in _PER_CALL_CLASSES)
                )
                if at_startup:
                    found.append((key, f"{rel}:{node.lineno}"))
            for child in ast.iter_child_nodes(node):
                walk(child, classes, funcs)

        walk(tree, [], [])
    return found


class TestRestartRegistryMatchesTheSource(unittest.TestCase):
    def test_every_setting_read_at_startup_requires_a_restart(self):
        missing = sorted({f"{key} ({where})" for key, where in _startup_reads()
                          if key not in RESTART_REQUIRED_SETTINGS})
        self.assertEqual(missing, [], "read at startup but saved without a restart — add to "
                                      "RESTART_REQUIRED_SETTINGS or read it per call")

    def test_the_scan_finds_the_known_startup_reads(self):
        keys = {key for key, _where in _startup_reads()}
        for key in ('SYNC_PERIOD_MINS', 'EBOOK_CACHE_SIZE', 'ABS_PROGRESS_OFFSET_SECONDS', 'TELEGRAM_CHAT_ID'):
            self.assertIn(key, keys)

    def test_restart_settings_are_registered(self):
        self.assertEqual(sorted(RESTART_REQUIRED_SETTINGS - KNOWN_SETTING_KEYS), [])


if __name__ == '__main__':
    unittest.main()
