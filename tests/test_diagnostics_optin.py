"""Tests for diagnostics opt-in UX: API route, dashboard modal, and settings boolean."""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))


class MockContainer:
    """Minimal mock container for diagnostics opt-in route tests."""

    def __init__(self):
        self.mock_database_service = Mock()
        self.mock_database_service.get_all_settings.return_value = {}
        self.mock_database_service.get_all_books.return_value = []
        self.mock_database_service.get_all_states.return_value = []
        self.mock_database_service.get_all_hardcover_details.return_value = []
        self.mock_database_service.get_all_storygraph_details.return_value = []
        self.mock_database_service.get_all_reading_stats.return_value = []
        self.mock_database_service.get_all_booklore_books.return_value = []
        self.mock_database_service.get_all_pending_suggestions.return_value = []
        self.mock_database_service.get_books_by_status.return_value = []
        self.mock_database_service.list_users.return_value = []

    def database_service(self):
        return self.mock_database_service

    def sync_manager(self):
        return Mock()

    def abs_client(self):
        return Mock()

    def booklore_client(self):
        return Mock()

    def bookfusion_client(self):
        return Mock()

    def storyteller_client(self):
        return Mock()

    def hardcover_client(self):
        return Mock()

    def storygraph_client(self):
        return Mock()

    def bookorbit_client(self):
        return Mock()

    def cwa_client(self):
        return Mock()

    def ebook_parser(self):
        return Mock()

    def sync_clients(self):
        return {}

    def forge_service(self):
        m = Mock()
        m.active_tasks = set()
        return m

    def user_client_registry(self):
        return Mock()

    def data_dir(self):
        return Path(tempfile.gettempdir())

    def books_dir(self):
        return Path(tempfile.gettempdir())

    def epub_cache_dir(self):
        return Path(tempfile.gettempdir()) / 'test_epub_cache'


class _DiagnosticsOptInBase(unittest.TestCase):
    """Shared setUp/tearDown for env hygiene and app creation."""

    def setUp(self):
        self._env_snapshot = dict(os.environ)
        self._tmp = tempfile.mkdtemp()
        os.environ['DATA_DIR'] = self._tmp
        for key in ('DIAGNOSTICS_OPT_IN', 'DIAGNOSTICS_PROMPTED',
                     'DIAGNOSTICS_INSTANCE_ID', 'DIAGNOSTICS_ENDPOINT_URL',
                     'DIAGNOSTICS_LAST_SENT'):
            os.environ.pop(key, None)

        self.container = MockContainer()
        import src.db.migration_utils
        self._orig_init = src.db.migration_utils.initialize_database
        src.db.migration_utils.initialize_database = lambda data_dir: self.container.database_service()

        from src.web_server import create_app
        self.app, _ = create_app(test_container=self.container)
        self.app.config['TESTING'] = True
        self.client = self.app.test_client()

    def tearDown(self):
        import src.db.migration_utils
        src.db.migration_utils.initialize_database = self._orig_init
        os.environ.clear()
        os.environ.update(self._env_snapshot)
        shutil.rmtree(self._tmp, ignore_errors=True)


class TestOptInRoute(_DiagnosticsOptInBase):
    """POST /api/diagnostics/opt-in tests."""

    def test_opt_in_true(self):
        resp = self.client.post(
            '/api/diagnostics/opt-in',
            json={'opt_in': True},
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data['ok'])
        self.assertTrue(data['opt_in'])
        self.assertIsInstance(data['instance_id'], str)
        self.assertEqual(len(data['instance_id']), 32)
        self.assertEqual(os.environ.get('DIAGNOSTICS_OPT_IN'), 'true')
        self.assertEqual(os.environ.get('DIAGNOSTICS_PROMPTED'), 'true')
        db = self.container.database_service()
        db.set_setting.assert_any_call('DIAGNOSTICS_OPT_IN', 'true')
        db.set_setting.assert_any_call('DIAGNOSTICS_PROMPTED', 'true')
        db.set_setting.assert_any_call('DIAGNOSTICS_INSTANCE_ID', data['instance_id'])

    def test_opt_in_false(self):
        resp = self.client.post(
            '/api/diagnostics/opt-in',
            json={'opt_in': False},
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data['ok'])
        self.assertFalse(data['opt_in'])
        self.assertEqual(data['instance_id'], '')
        self.assertEqual(os.environ.get('DIAGNOSTICS_OPT_IN'), 'false')
        self.assertEqual(os.environ.get('DIAGNOSTICS_PROMPTED'), 'true')
        self.assertNotIn('DIAGNOSTICS_INSTANCE_ID', os.environ)


class TestDashboardModal(_DiagnosticsOptInBase):
    """GET / diagnostics modal visibility tests."""

    def _get_index_render_args(self):
        """Fetch / and capture render_template kwargs."""
        import src.web_server
        original_render = src.web_server.render_template
        captured = {}
        def capture_render(template_name, **kwargs):
            captured.update(kwargs)
            return 'OK'
        src.web_server.render_template = capture_render
        try:
            self.client.get('/')
            return captured
        finally:
            src.web_server.render_template = original_render

    def test_modal_shown_when_not_prompted(self):
        os.environ.pop('DIAGNOSTICS_PROMPTED', None)
        os.environ['DIAGNOSTICS_ENDPOINT_URL'] = 'http://collector.example.com/api/v1/diagnostics'
        ctx = self._get_index_render_args()
        self.assertTrue(ctx.get('show_diagnostics_modal'))

    def test_modal_hidden_when_prompted(self):
        os.environ['DIAGNOSTICS_PROMPTED'] = 'true'
        os.environ['DIAGNOSTICS_ENDPOINT_URL'] = 'http://collector.example.com/api/v1/diagnostics'
        ctx = self._get_index_render_args()
        self.assertFalse(ctx.get('show_diagnostics_modal'))

    def test_modal_hidden_when_no_endpoint(self):
        os.environ.pop('DIAGNOSTICS_PROMPTED', None)
        os.environ.pop('DIAGNOSTICS_ENDPOINT_URL', None)
        ctx = self._get_index_render_args()
        self.assertFalse(ctx.get('show_diagnostics_modal'))


class TestTemplateStructure(unittest.TestCase):
    """Regression: templates must have proper HTML closing tags."""

    def test_index_html_ends_with_closing_tags(self):
        index = Path(__file__).parent.parent / 'templates' / 'index.html'
        content = index.read_text(encoding='utf-8')
        stripped = content.rstrip()
        self.assertTrue(stripped.endswith('</html>'),
                        f"index.html must end with </html>, got: ...{stripped[-40:]}")
        self.assertIn('</body>', stripped)


class TestSettingsBoolean(_DiagnosticsOptInBase):
    """Settings POST boolean-save tests for DIAGNOSTICS_OPT_IN."""

    def _settings_store(self):
        store = {}
        db = self.container.database_service()
        db.get_all_settings.return_value = store
        db.set_setting.side_effect = lambda k, v: store.__setitem__(k, v)
        return store

    @patch('src.web_server.restart_server')
    def test_checkbox_absent_saves_false(self, mock_restart):
        store = self._settings_store()
        resp = self.client.post('/settings', data={})
        self.assertIn(resp.status_code, (200, 302))
        self.assertEqual(store.get('DIAGNOSTICS_OPT_IN'), 'false')
        self.assertEqual(os.environ.get('DIAGNOSTICS_OPT_IN'), 'false')

    @patch('src.web_server.restart_server')
    def test_checkbox_present_saves_true(self, mock_restart):
        store = self._settings_store()
        resp = self.client.post('/settings', data={'DIAGNOSTICS_OPT_IN': 'on'})
        self.assertIn(resp.status_code, (200, 302))
        self.assertEqual(store.get('DIAGNOSTICS_OPT_IN'), 'true')
        self.assertEqual(os.environ.get('DIAGNOSTICS_OPT_IN'), 'true')


if __name__ == '__main__':
    unittest.main()
