"""Tests for the compact, instance-level diagnostics feedback history."""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src.web_server import _diagnostics_receiver_context
from tests.test_webserver import MockContainer


class _Base(unittest.TestCase):
    """Create a test app while preserving process-wide diagnostics settings."""

    def setUp(self):
        self._env_snapshot = dict(os.environ)
        self._tmp = tempfile.mkdtemp()
        os.environ['DATA_DIR'] = self._tmp
        os.environ['TEMPLATE_DIR'] = str(
            Path(__file__).parent.parent / 'templates',
        )
        for key in (
            'DIAGNOSTICS_OPT_IN',
            'DIAGNOSTICS_PROMPTED',
            'DIAGNOSTICS_INSTANCE_ID',
            'DIAGNOSTICS_ENDPOINT_URL',
            'DIAGNOSTICS_INGEST_TOKEN',
            'DIAGNOSTICS_LAST_SENT',
        ):
            os.environ.pop(key, None)

        self.container = MockContainer()
        self.container.mock_database_service.get_all_settings.return_value = {}
        import src.db.migration_utils
        self._orig_init = src.db.migration_utils.initialize_database
        src.db.migration_utils.initialize_database = (
            lambda data_dir: self.container.database_service()
        )

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


class TestReceiverContext(unittest.TestCase):
    """The proxy derives only a safe receiver origin and keeps its token private."""

    def test_requires_endpoint_and_token(self):
        os.environ.pop('DIAGNOSTICS_ENDPOINT_URL', None)
        os.environ.pop('DIAGNOSTICS_INGEST_TOKEN', None)
        base, token, error = _diagnostics_receiver_context()
        self.assertEqual((base, token), ('', ''))
        self.assertIn('not configured', error)

        os.environ['DIAGNOSTICS_ENDPOINT_URL'] = (
            'https://example.com/api/v1/diagnostics'
        )
        base, token, error = _diagnostics_receiver_context()
        self.assertEqual((base, token), ('', ''))
        self.assertIn('token', error.lower())

    def test_derives_origin_and_rejects_unsafe_urls(self):
        os.environ['DIAGNOSTICS_ENDPOINT_URL'] = (
            'https://example.com/some/untrusted/path'
        )
        os.environ['DIAGNOSTICS_INGEST_TOKEN'] = 'tok123'
        self.assertEqual(
            _diagnostics_receiver_context(),
            ('https://example.com', 'tok123', ''),
        )

        os.environ['DIAGNOSTICS_ENDPOINT_URL'] = 'file://receiver/diagnostics'
        base, _token, error = _diagnostics_receiver_context()
        self.assertEqual(base, '')
        self.assertIn('invalid', error.lower())


class TestSubmissionsProxy(_Base):
    """GET /api/diagnostics/submissions proxies only safe history fields."""

    def test_unconfigured_returns_503(self):
        response = self.client.get('/api/diagnostics/submissions')
        self.assertEqual(response.status_code, 503)

    def test_no_token_yet_returns_empty_history(self):
        os.environ['DIAGNOSTICS_ENDPOINT_URL'] = (
            'https://rx.example.com/api/v1/diagnostics'
        )
        response = self.client.get('/api/diagnostics/submissions')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {'submissions': []})

    @patch('src.web_server.requests')
    def test_forwards_bearer_and_whitelists_response(self, mock_requests):
        os.environ['DIAGNOSTICS_ENDPOINT_URL'] = (
            'https://rx.example.com/api/v1/diagnostics'
        )
        os.environ['DIAGNOSTICS_INGEST_TOKEN'] = 'instance-token'
        upstream = Mock(status_code=200)
        upstream.json.return_value = {
            'submissions': [{
                'id': 42,
                'received_at': '2026-07-16T12:00:00Z',
                'user_message': 'Sync stopped.',
                'response_md': 'Thanks, this is fixed.',
                'response_at': '2026-07-16T14:00:00Z',
                'template': 'must not leak',
                'severity': 'high',
            }],
        }
        mock_requests.get.return_value = upstream

        response = self.client.get('/api/diagnostics/submissions')

        self.assertEqual(response.status_code, 200)
        mock_requests.get.assert_called_once_with(
            'https://rx.example.com/api/v1/my/submissions',
            headers={'Authorization': 'Bearer instance-token'},
            timeout=15,
        )
        data = response.get_json()
        self.assertEqual(data['submissions'][0], {
            'id': 42,
            'submitted_at': '2026-07-16T12:00:00Z',
            'user_message': 'Sync stopped.',
            'response_md': 'Thanks, this is fixed.',
            'response_at': '2026-07-16T14:00:00Z',
            'status': 'replied',
        })
        rendered = response.data.decode()
        self.assertNotIn('must not leak', rendered)
        self.assertNotIn('instance-token', rendered)
        self.assertNotIn('rx.example.com', rendered)

    @patch('src.web_server.requests')
    def test_upstream_error_returns_502(self, mock_requests):
        os.environ['DIAGNOSTICS_ENDPOINT_URL'] = (
            'https://rx.example.com/api/v1/diagnostics'
        )
        os.environ['DIAGNOSTICS_INGEST_TOKEN'] = 'tok'
        mock_requests.get.return_value = Mock(status_code=500)
        response = self.client.get('/api/diagnostics/submissions')
        self.assertEqual(response.status_code, 502)
        self.assertNotIn('tok', response.data.decode())

    @patch('src.web_server.requests')
    def test_request_exception_returns_502(self, mock_requests):
        import requests as real_requests

        os.environ['DIAGNOSTICS_ENDPOINT_URL'] = (
            'https://rx.example.com/api/v1/diagnostics'
        )
        os.environ['DIAGNOSTICS_INGEST_TOKEN'] = 'tok'
        mock_requests.RequestException = real_requests.RequestException
        mock_requests.get.side_effect = real_requests.ConnectionError('offline')
        response = self.client.get('/api/diagnostics/submissions')
        self.assertEqual(response.status_code, 502)

    @patch('src.web_server.requests')
    def test_invalid_upstream_json_returns_502(self, mock_requests):
        os.environ['DIAGNOSTICS_ENDPOINT_URL'] = (
            'https://rx.example.com/api/v1/diagnostics'
        )
        os.environ['DIAGNOSTICS_INGEST_TOKEN'] = 'tok'
        upstream = Mock(status_code=200)
        upstream.json.side_effect = ValueError('bad json')
        mock_requests.get.return_value = upstream
        response = self.client.get('/api/diagnostics/submissions')
        self.assertEqual(response.status_code, 502)


class TestRoutes(_Base):
    """The old technical page redirects and feedback history remains admin-only."""

    def test_my_reports_redirects_to_diagnostics_settings(self):
        response = self.client.get('/my-reports')
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers['Location'].endswith('/settings#system'))

    def test_route_registration_and_admin_guards(self):
        with self.app.test_request_context():
            from flask import url_for
            self.assertEqual(
                url_for('api_diagnostics_submissions'),
                '/api/diagnostics/submissions',
            )
            self.assertEqual(url_for('my_reports'), '/my-reports')

        from src.web_server import _ADMIN_ONLY_ENDPOINTS
        self.assertIn('api_diagnostics_submissions', _ADMIN_ONLY_ENDPOINTS)
        self.assertIn('my_reports', _ADMIN_ONLY_ENDPOINTS)
        rules = {rule.rule for rule in self.app.url_map.iter_rules()}
        self.assertNotIn(
            '/api/diagnostics/findings/<int:finding_id>/comments',
            rules,
        )

    def test_non_admin_cannot_send_or_read_instance_reports(self):
        user = Mock(id=2, active=True, is_admin=False)
        self.container.mock_database_service.count_users.return_value = 1
        self.container.mock_database_service.get_user.return_value = user
        self.container.mock_database_service.get_user_credentials.return_value = {}
        self.app.config['LOGIN_DISABLED'] = False
        with self.client.session_transaction() as session:
            session['user_id'] = user.id

        history = self.client.get(
            '/api/diagnostics/submissions',
            headers={'Accept': 'application/json'},
        )
        send = self.client.post(
            '/api/diagnostics/send-now',
            json={'message': 'private'},
            headers={'Accept': 'application/json'},
        )

        self.assertEqual(history.status_code, 403)
        self.assertEqual(send.status_code, 403)


class TestTemplates(unittest.TestCase):
    """The dashboard is gone and Settings renders only compact safe history."""

    def setUp(self):
        self.templates = Path(__file__).parent.parent / 'templates'

    def test_dashboard_has_no_my_reports_link(self):
        content = (self.templates / 'index.html').read_text(encoding='utf-8')
        self.assertNotIn('My Reports', content)

    def test_settings_has_accessible_manual_report_controls(self):
        content = (self.templates / 'settings.html').read_text(encoding='utf-8')
        self.assertIn('What went wrong? (optional)', content)
        self.assertIn('maxlength="2000"', content)
        self.assertIn('Send bug report', content)
        self.assertIn('Recent submitted reports', content)
        self.assertIn('aria-live="polite"', content)
        self.assertNotIn('warning_count || 0', content)

        start = content.index('function _renderDiagnosticsSubmissions')
        end = content.index('function togglePollSeconds')
        history_script = content[start:end]
        self.assertIn('textContent', history_script)
        self.assertNotIn('innerHTML', history_script)

    def test_retired_template_removed(self):
        self.assertFalse((self.templates / 'my_reports.html').exists())


if __name__ == '__main__':
    unittest.main()
