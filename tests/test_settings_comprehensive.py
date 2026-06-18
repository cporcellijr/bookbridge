
import unittest
import tempfile
import os
import re
from pathlib import Path
from unittest.mock import Mock, patch
import sys

# Add project root to Python path
sys.path.insert(0, str(Path(__file__).parent.parent))

class MockContainer:
    """Mock container for testing."""
    def __init__(self):
        self.mock_database_service = Mock()
        self.mock_sync_manager = Mock()
        self.mock_sync_manager.get_abs_title.return_value = 'Test'
        
    def database_service(self): return self.mock_database_service
    def sync_manager(self): return self.mock_sync_manager
    def abs_client(self): return Mock()
    def booklore_client(self): return Mock()
    def storyteller_client(self): return Mock()
    def hardcover_client(self): return Mock()
    def storygraph_client(self): return Mock()
    def transcriber(self): return Mock()
    def ebook_parser(self): return Mock()
    def sync_clients(self): return {}
    def data_dir(self): return Path(tempfile.gettempdir())
    def books_dir(self): return Path(tempfile.gettempdir())
    def epub_cache_dir(self): return Path(tempfile.gettempdir()) / 'test_epub_cache'

class TestSettingsComprehensive(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        os.environ['DATA_DIR'] = self.temp_dir
        self.settings_store = {}
        
        self.mock_container = MockContainer()
        self.mock_container.mock_database_service.get_all_settings.side_effect = lambda: dict(self.settings_store)
        self.mock_container.mock_database_service.set_setting.side_effect = (
            lambda key, value: self.settings_store.__setitem__(key, value)
        )
        
        # Mock database initialization
        def mock_init_db(data_dir):
            return self.mock_container.mock_database_service
            
        import src.db.migration_utils
        self.original_init_db = src.db.migration_utils.initialize_database
        src.db.migration_utils.initialize_database = mock_init_db

        from src.web_server import create_app
        self.app, _ = create_app(test_container=self.mock_container)
        self.app.config['TESTING'] = True
        self.client = self.app.test_client()

        # List of all boolean keys from web_server.py
        self.bool_keys = [
            'KOSYNC_USE_PERCENTAGE_FROM_SERVER',
            'SYNC_ABS_EBOOK',
            'XPATH_FALLBACK_TO_PREVIOUS_SEGMENT',
            'KOSYNC_ENABLED',
            'STORYTELLER_ENABLED',
            'BOOKLORE_ENABLED',
            'GRIMMORY_READING_SESSIONS',
            'CWA_ENABLED',
            'CWA_SYNC_ENABLED',
            'HARDCOVER_ENABLED',
            'STORYGRAPH_ENABLED',
            'TELEGRAM_ENABLED',
            'SUGGESTIONS_ENABLED',
            'ABS_ONLY_SEARCH_IN_ABS_LIBRARY_ID',
            'REPROCESS_ON_CLEAR_IF_NO_ALIGNMENT',
            'INSTANT_SYNC_ENABLED',
            'SHELFMARK_ENABLED',
            'STORYTELLER_NO_EPUB_CACHE',
            'BOOKLORE_SHELF_WATCH_ENABLED',
            'BOOKORBIT_ENABLED',
            'BOOKORBIT_READING_SESSIONS',
            'BOOKORBIT_SHELF_WATCH_ENABLED',
            'CALIBRE_USE_ABS_IDENTIFIER',
        ]

    def tearDown(self):
        import src.db.migration_utils
        src.db.migration_utils.initialize_database = self.original_init_db
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        # Clear env vars
        for key in self.bool_keys:
            if key in os.environ:
                del os.environ[key]

    def _render_settings_template_source(self):
        import src.web_server
        template_source = (Path(__file__).parent.parent / 'templates' / 'settings.html').read_text(encoding='utf-8')
        original_render = src.web_server.render_template

        def render_from_source(_template_name, **context):
            return src.web_server.render_template_string(template_source, **context)

        src.web_server.render_template = render_from_source
        try:
            response = self.client.get('/settings')
            self.assertEqual(response.status_code, 200)
            return response.get_data(as_text=True)
        finally:
            src.web_server.render_template = original_render

    @patch('src.web_server.restart_server')
    def test_all_bool_toggles(self, mock_restart):
        """Verify EVERY boolean setting can be toggled ON and OFF."""
        
        # 1. Turn EVERYTHING ON
        # Construct form data with all keys present (simulating checked checkboxes)
        data_on = {key: 'on' for key in self.bool_keys}
        # Add a required non-bool field so validation passes if any
        data_on['SYNC_PERIOD_MINS'] = '5'
        
        response = self.client.post('/settings', data=data_on)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Restarting the application', response.data)
        
        # Verify calls to set_setting with 'true'
        for key in self.bool_keys:
            self.mock_container.mock_database_service.set_setting.assert_any_call(key, 'true')
            self.assertEqual(os.environ.get(key), 'true', f"{key} should be 'true' in env")

        # Reset mock calls for clean check
        self.mock_container.mock_database_service.reset_mock()

        # 2. Turn EVERYTHING OFF
        # Construct form data with NONE of the keys (simulating unchecked checkboxes)
        data_off = {
            'SYNC_PERIOD_MINS': '5'
        }
        
        response = self.client.post('/settings', data=data_off)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Restarting the application', response.data)
        
        # Verify calls to set_setting with 'false'
        for key in self.bool_keys:
            self.mock_container.mock_database_service.set_setting.assert_any_call(key, 'false')
            self.assertEqual(os.environ.get(key), 'false', f"{key} should be 'false' in env")

    @patch('src.web_server.restart_server')
    def test_text_fields_save(self, mock_restart):
        """Verify text fields correspond to logic."""
        test_data = {
            'TZ': 'Europe/Paris',
            'SYNC_PERIOD_MINS': '15',
            'ABS_SERVER': 'http://test.com'
        }
        
        response = self.client.post('/settings', data=test_data)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Restarting the application', response.data)
        
        for key, val in test_data.items():
            self.mock_container.mock_database_service.set_setting.assert_any_call(key, val)

    @patch('src.web_server.restart_server')
    def test_storygraph_enabled_persists_after_save_and_reload(self, mock_restart):
        response = self.client.post('/settings', data={
            'SYNC_PERIOD_MINS': '5',
            'STORYGRAPH_ENABLED': 'on',
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Restarting the application', response.data)
        self.assertEqual(self.settings_store.get('STORYGRAPH_ENABLED'), 'true')

        os.environ.pop('STORYGRAPH_ENABLED', None)

        from src.utils.config_loader import ConfigLoader
        ConfigLoader.load_settings(self.mock_container.mock_database_service)

        html = self._render_settings_template_source()
        self.assertRegex(
            html,
            re.compile(
                r'<input type="checkbox" id="toggle_storygraph" name="STORYGRAPH_ENABLED"[\s\S]*?checked',
                re.IGNORECASE,
            ),
        )

    def test_settings_get_renders_custom_whisper_model_as_text_value(self):
        with patch.dict(os.environ, {'WHISPER_MODEL': 'custom-q5_k_m'}, clear=False):
            html = self._render_settings_template_source()

        self.assertIn('name="WHISPER_MODEL"', html)
        self.assertIn('list="whisper-model-suggestions"', html)
        self.assertIn('value="custom-q5_k_m"', html)
        self.assertIn('<datalist id="whisper-model-suggestions">', html)
        self.assertIn('<option value="tiny"></option>', html)
        self.assertIn('<option value="large-v3"></option>', html)

    def test_settings_get_renders_selectable_library_pickers(self):
        html = self._render_settings_template_source()

        self.assertIn('name="ABS_LIBRARY_ID"', html)
        self.assertIn('onclick="checkAbsLibs()"', html)
        self.assertIn("fetch('/api/abs/libraries')", html)
        self.assertIn('id="abs_library_picker"', html)
        self.assertIn("applyLibraryPickerSelection('ABS_LIBRARY_ID', 'abs_library_picker')", html)
        self.assertIn('Use when restricting ABS search to a specific library.', html)
        self.assertIn('name="BOOKLORE_LIBRARY_ID"', html)
        self.assertIn('onclick="checkBookloreLibs()"', html)
        self.assertIn("fetch('/api/booklore/libraries')", html)
        self.assertIn('id="booklore_library_picker"', html)
        self.assertIn("applyLibraryPickerSelection('BOOKLORE_LIBRARY_ID', 'booklore_library_picker')", html)
        self.assertIn('Select a library to fill the ID field.', html)

    @patch('src.web_server.restart_server')
    def test_custom_whisper_model_is_saved_without_being_forced_to_builtin(self, mock_restart):
        response = self.client.post('/settings', data={
            'SYNC_PERIOD_MINS': '5',
            'WHISPER_MODEL': 'custom-q5_k_m'
        })

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Restarting the application', response.data)
        self.mock_container.mock_database_service.set_setting.assert_any_call('WHISPER_MODEL', 'custom-q5_k_m')
        self.assertEqual(os.environ.get('WHISPER_MODEL'), 'custom-q5_k_m')

if __name__ == '__main__':
    unittest.main()
