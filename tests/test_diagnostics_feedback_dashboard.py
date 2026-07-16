"""Tests for diagnostics feedback dashboard: My Reports view and comment proxy."""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.web_server import _diagnostics_receiver_context
from tests.test_webserver import MockContainer


class _Base(unittest.TestCase):
    """Shared setUp/tearDown for env hygiene and app creation."""

    def setUp(self):
        self._env_snapshot = dict(os.environ)
        self._tmp = tempfile.mkdtemp()
        os.environ['DATA_DIR'] = self._tmp
        os.environ['TEMPLATE_DIR'] = str(
            Path(__file__).parent.parent / 'templates',
        )
        for key in ('DIAGNOSTICS_OPT_IN', 'DIAGNOSTICS_PROMPTED',
                     'DIAGNOSTICS_INSTANCE_ID', 'DIAGNOSTICS_ENDPOINT_URL',
                     'DIAGNOSTICS_INGEST_TOKEN', 'DIAGNOSTICS_LAST_SENT'):
            os.environ.pop(key, None)

        self.container = MockContainer()
        self.container.mock_database_service.get_all_settings.return_value = {}
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


class TestReceiverContext(unittest.TestCase):
    """Unit tests for _diagnostics_receiver_context helper."""

    def test_empty_endpoint(self):
        os.environ.pop('DIAGNOSTICS_ENDPOINT_URL', None)
        os.environ.pop('DIAGNOSTICS_INGEST_TOKEN', None)
        base, token, err = _diagnostics_receiver_context()
        self.assertEqual(base, '')
        self.assertEqual(token, '')
        self.assertIn('not configured', err)

    def test_empty_token(self):
        os.environ['DIAGNOSTICS_ENDPOINT_URL'] = 'https://example.com/api/v1/diagnostics'
        os.environ.pop('DIAGNOSTICS_INGEST_TOKEN', None)
        base, token, err = _diagnostics_receiver_context()
        self.assertEqual(base, '')
        self.assertEqual(token, '')
        self.assertIn('token', err.lower())

    def test_derives_origin_robustly(self):
        os.environ['DIAGNOSTICS_ENDPOINT_URL'] = 'https://example.com/some/attacker/path'
        os.environ['DIAGNOSTICS_INGEST_TOKEN'] = 'tok123'
        base, token, err = _diagnostics_receiver_context()
        self.assertEqual(base, 'https://example.com')
        self.assertEqual(token, 'tok123')
        self.assertEqual(err, '')

    def test_invalid_url_no_netloc(self):
        os.environ['DIAGNOSTICS_ENDPOINT_URL'] = 'not-a-url'
        os.environ['DIAGNOSTICS_INGEST_TOKEN'] = 'tok'
        base, token, err = _diagnostics_receiver_context()
        self.assertEqual(base, '')
        self.assertIn('invalid', err.lower())

    def test_rejects_non_http_scheme(self):
        os.environ['DIAGNOSTICS_ENDPOINT_URL'] = 'file://receiver/api/v1/diagnostics'
        os.environ['DIAGNOSTICS_INGEST_TOKEN'] = 'tok'
        base, _token, err = _diagnostics_receiver_context()
        self.assertEqual(base, '')
        self.assertIn('invalid', err.lower())


class TestMyReportsView(_Base):
    """GET /my-reports tests."""

    def test_unconfigured_renders_friendly_state(self):
        resp = self.client.get('/my-reports')
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode()
        self.assertIn('not configured', html.lower())
        self.assertNotIn('<div class="finding-card"', html)

    @patch('src.web_server.requests')
    def test_configured_sends_bearer_and_renders_findings(self, mock_requests):
        os.environ['DIAGNOSTICS_ENDPOINT_URL'] = 'https://rx.example.com/api/v1/diagnostics'
        os.environ['DIAGNOSTICS_INGEST_TOKEN'] = 'test-token-abc'
        mock_resp = Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            'findings': [{
                'id': 42, 'category': 'code-bug', 'severity': 'high',
                'status': 'open', 'total_count': 5, 'last_seen': '2026-07-15T10:00:00Z',
                'template': 'Some warning text',
                'response_md': 'Fixed in v7.3', 'response_at': '2026-07-15T12:00:00Z',
                'comments': [
                    {'body': 'Visible comment', 'is_mine': True, 'created_at': '2026-07-15T11:00:00Z'},
                    {'body': 'From contributor', 'is_mine': False, 'created_at': '2026-07-15T11:30:00Z'},
                ],
            }],
        }
        mock_requests.get.return_value = mock_resp
        resp = self.client.get('/my-reports')
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode()
        # Bearer header sent
        mock_requests.get.assert_called_once()
        call_kwargs = mock_requests.get.call_args
        self.assertEqual(
            call_kwargs[1]['headers']['Authorization'], 'Bearer test-token-abc',
        )
        self.assertIn('https://rx.example.com/api/v1/my/findings', call_kwargs[0][0])
        # Findings rendered
        self.assertIn('#42', html)
        self.assertIn('Some warning text', html)
        self.assertIn('Count: 5', html)
        self.assertIn('Fixed in v7.3', html)
        self.assertIn('Visible comment', html)
        self.assertIn('From contributor', html)
        self.assertIn('Mine', html)
        self.assertIn('Contributor', html)
        # Token not leaked to rendered output
        self.assertNotIn('test-token-abc', html)
        self.assertNotIn('rx.example.com', html)

    @patch('src.web_server.requests')
    def test_upstream_failure_renders_safely(self, mock_requests):
        os.environ['DIAGNOSTICS_ENDPOINT_URL'] = 'https://rx.example.com/api/v1/diagnostics'
        os.environ['DIAGNOSTICS_INGEST_TOKEN'] = 'tok'
        mock_requests.get.return_value = Mock(status_code=500)
        resp = self.client.get('/my-reports')
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode()
        self.assertIn('error', html.lower())
        self.assertNotIn('tok', html)

    @patch('src.web_server.requests')
    def test_request_exception_renders_safely(self, mock_requests):
        os.environ['DIAGNOSTICS_ENDPOINT_URL'] = 'https://rx.example.com/api/v1/diagnostics'
        os.environ['DIAGNOSTICS_INGEST_TOKEN'] = 'tok'
        import requests as _req
        mock_requests.RequestException = _req.RequestException
        mock_requests.get.side_effect = _req.ConnectionError('fail')
        resp = self.client.get('/my-reports')
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode()
        self.assertIn('Could not reach', html)
        self.assertNotIn('tok', html)


class TestCommentProxy(_Base):
    """POST /api/diagnostics/findings/<id>/comments tests."""

    def test_unconfigured_returns_503(self):
        resp = self.client.post(
            '/api/diagnostics/findings/1/comments',
            json={'body': 'hi'}, content_type='application/json',
        )
        self.assertEqual(resp.status_code, 503)

    def test_validation_missing_body_key(self):
        os.environ['DIAGNOSTICS_ENDPOINT_URL'] = 'https://rx.example.com/api/v1/diagnostics'
        os.environ['DIAGNOSTICS_INGEST_TOKEN'] = 'tok'
        resp = self.client.post(
            '/api/diagnostics/findings/1/comments',
            json={'other': 'val'}, content_type='application/json',
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn('body', resp.get_json()['error'].lower())

    def test_validation_body_not_string(self):
        os.environ['DIAGNOSTICS_ENDPOINT_URL'] = 'https://rx.example.com/api/v1/diagnostics'
        os.environ['DIAGNOSTICS_INGEST_TOKEN'] = 'tok'
        resp = self.client.post(
            '/api/diagnostics/findings/1/comments',
            json={'body': 123}, content_type='application/json',
        )
        self.assertEqual(resp.status_code, 400)

    def test_validation_empty_body(self):
        os.environ['DIAGNOSTICS_ENDPOINT_URL'] = 'https://rx.example.com/api/v1/diagnostics'
        os.environ['DIAGNOSTICS_INGEST_TOKEN'] = 'tok'
        resp = self.client.post(
            '/api/diagnostics/findings/1/comments',
            json={'body': '   '}, content_type='application/json',
        )
        self.assertEqual(resp.status_code, 400)

    def test_validation_no_json(self):
        os.environ['DIAGNOSTICS_ENDPOINT_URL'] = 'https://rx.example.com/api/v1/diagnostics'
        os.environ['DIAGNOSTICS_INGEST_TOKEN'] = 'tok'
        resp = self.client.post(
            '/api/diagnostics/findings/1/comments',
            data='not json', content_type='text/plain',
        )
        self.assertEqual(resp.status_code, 400)

    @patch('src.web_server.requests')
    def test_proxies_exact_url_body_header_status(self, mock_requests):
        os.environ['DIAGNOSTICS_ENDPOINT_URL'] = 'https://rx.example.com/api/v1/diagnostics'
        os.environ['DIAGNOSTICS_INGEST_TOKEN'] = 'my-token'
        mock_resp = Mock()
        mock_resp.status_code = 201
        mock_resp.json.return_value = {'ok': True, 'comment_id': 7}
        mock_requests.post.return_value = mock_resp
        resp = self.client.post(
            '/api/diagnostics/findings/42/comments',
            json={'body': 'hello world'}, content_type='application/json',
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.get_json(), {'ok': True, 'comment_id': 7})
        mock_requests.post.assert_called_once()
        call_args = mock_requests.post.call_args
        self.assertEqual(call_args[0][0], 'https://rx.example.com/api/v1/findings/42/comments')
        self.assertEqual(call_args[1]['json'], {'body': 'hello world'})
        self.assertEqual(call_args[1]['headers']['Authorization'], 'Bearer my-token')
        self.assertEqual(call_args[1]['timeout'], 15)

    @patch('src.web_server.requests')
    def test_upstream_exception_returns_502(self, mock_requests):
        os.environ['DIAGNOSTICS_ENDPOINT_URL'] = 'https://rx.example.com/api/v1/diagnostics'
        os.environ['DIAGNOSTICS_INGEST_TOKEN'] = 'tok'
        import requests as _req
        mock_requests.RequestException = _req.RequestException
        mock_requests.post.side_effect = _req.ConnectionError('down')
        resp = self.client.post(
            '/api/diagnostics/findings/1/comments',
            json={'body': 'test'}, content_type='application/json',
        )
        self.assertEqual(resp.status_code, 502)
        self.assertIn('error', resp.get_json())

    @patch('src.web_server.requests')
    def test_upstream_non_json_response(self, mock_requests):
        os.environ['DIAGNOSTICS_ENDPOINT_URL'] = 'https://rx.example.com/api/v1/diagnostics'
        os.environ['DIAGNOSTICS_INGEST_TOKEN'] = 'tok'
        mock_resp = Mock()
        mock_resp.status_code = 200
        mock_resp.json.side_effect = ValueError('bad json')
        mock_requests.post.return_value = mock_resp
        resp = self.client.post(
            '/api/diagnostics/findings/1/comments',
            json={'body': 'test'}, content_type='application/json',
        )
        self.assertEqual(resp.status_code, 502)


class TestRouteRegistration(_Base):
    """Verify routes are registered and admin-only."""

    def test_my_reports_route_exists(self):
        with self.app.test_request_context():
            from flask import url_for
            url = url_for('my_reports')
            self.assertEqual(url, '/my-reports')

    def test_comment_route_exists(self):
        with self.app.test_request_context():
            from flask import url_for
            url = url_for('api_diagnostics_finding_comment', finding_id=5)
            self.assertEqual(url, '/api/diagnostics/findings/5/comments')

    def test_my_reports_in_admin_only_endpoints(self):
        from src.web_server import _ADMIN_ONLY_ENDPOINTS
        self.assertIn('my_reports', _ADMIN_ONLY_ENDPOINTS)
        self.assertIn('api_diagnostics_finding_comment', _ADMIN_ONLY_ENDPOINTS)


class TestTemplateAndNavigation(unittest.TestCase):
    """Verify template structure and admin nav containment."""

    def setUp(self):
        self._templates = Path(__file__).parent.parent / 'templates'

    def test_my_reports_template_no_unsafe_filter(self):
        content = (self._templates / 'my_reports.html').read_text(encoding='utf-8')
        self.assertNotIn('|safe', content)
        self.assertTrue(content.strip().endswith('</html>'))

    def test_my_reports_template_has_pre_wrap(self):
        content = (self._templates / 'my_reports.html').read_text(encoding='utf-8')
        self.assertIn('pre-wrap', content)

    def test_my_reports_template_has_labels(self):
        content = (self._templates / 'my_reports.html').read_text(encoding='utf-8')
        self.assertIn('aria-label', content)
        self.assertIn('role="status"', content)

    def test_my_reports_template_no_upstream_url(self):
        content = (self._templates / 'my_reports.html').read_text(encoding='utf-8')
        self.assertNotIn('DIAGNOSTICS_ENDPOINT_URL', content)
        self.assertNotIn('DIAGNOSTICS_INGEST_TOKEN', content)

    def test_index_html_my_reports_only_in_admin_block(self):
        index = self._templates / 'index.html'
        content = index.read_text(encoding='utf-8')
        # Find all occurrences of My Reports in the file
        lines = content.split('\n')
        for i, line in enumerate(lines):
            if 'My Reports' in line:
                # Check the surrounding context for is_admin guard
                # Look backwards for the nearest {% if is_admin %}
                found_guard = False
                for j in range(i - 1, max(i - 10, -1), -1):
                    if '{% if is_admin %}' in lines[j]:
                        found_guard = True
                        break
                    if '{% endif %}' in lines[j]:
                        break
                self.assertTrue(
                    found_guard,
                    f"My Reports link at line {i + 1} not inside an is_admin block",
                )


if __name__ == '__main__':
    unittest.main()
