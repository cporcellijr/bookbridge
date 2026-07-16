"""Tests for the diagnostics sender (Phase 2: payload builder, daily sender, admin endpoint)."""
import logging
import os
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock, Mock

from src.services.diagnostics import (
    DiagnosticsLogHandler,
    ensure_instance_id,
    build_diagnostics_payload,
    maybe_send_diagnostics,
    _utc_iso,
)


def _make_record(
    logger_name: str,
    level: int,
    message: str,
) -> logging.LogRecord:
    """Create a LogRecord for testing."""
    return logging.LogRecord(
        name=logger_name,
        level=level,
        pathname='test.py',
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )


class FakeDatabaseService:
    """Records set_setting calls without touching a real database."""

    def __init__(self):
        self.settings: dict = {}
        self.set_setting_calls: list = []

    def set_setting(self, key: str, value: str) -> None:
        self.settings[key] = value
        self.set_setting_calls.append((key, value))

    def get_books_by_status(self, status: str):
        return []


# ---------------------------------------------------------------------------
# ensure_instance_id
# ---------------------------------------------------------------------------

class TestEnsureInstanceId(unittest.TestCase):

    def setUp(self):
        self._orig = os.environ.pop('DIAGNOSTICS_INSTANCE_ID', None)

    def tearDown(self):
        if self._orig is not None:
            os.environ['DIAGNOSTICS_INSTANCE_ID'] = self._orig
        else:
            os.environ.pop('DIAGNOSTICS_INSTANCE_ID', None)

    def test_generates_and_persists_when_missing(self):
        db = FakeDatabaseService()
        result = ensure_instance_id(db)
        self.assertEqual(len(result), 32)
        self.assertEqual(os.environ.get('DIAGNOSTICS_INSTANCE_ID'), result)
        self.assertEqual(db.settings.get('DIAGNOSTICS_INSTANCE_ID'), result)
        self.assertTrue(any(k == 'DIAGNOSTICS_INSTANCE_ID' for k, _ in db.set_setting_calls))

    def test_returns_existing_without_regenerating(self):
        os.environ['DIAGNOSTICS_INSTANCE_ID'] = 'existing-id'
        db = FakeDatabaseService()
        result = ensure_instance_id(db)
        self.assertEqual(result, 'existing-id')
        self.assertEqual(db.set_setting_calls, [])


# ---------------------------------------------------------------------------
# build_diagnostics_payload
# ---------------------------------------------------------------------------

class TestBuildDiagnosticsPayload(unittest.TestCase):

    def test_schema_and_metadata_present(self):
        payload = build_diagnostics_payload(
            instance_id='abc',
            service_flags={'abs': True},
            total_books=10,
            snapshot={'window_start': 'w1', 'taken_at': 't1', 'dropped': 2, 'entries': []},
        )
        self.assertEqual(payload['schema'], 1)
        self.assertEqual(payload['instance_id'], 'abc')
        self.assertIn('sent_at', payload)
        self.assertIn('app_version', payload)
        self.assertEqual(payload['services'], {'abs': True})
        self.assertEqual(payload['total_books'], 10)
        self.assertEqual(payload['window'], {'start': 'w1', 'end': 't1'})
        self.assertEqual(payload['dropped'], 2)
        self.assertEqual(payload['warnings'], [])

    def test_warnings_copied_from_entries(self):
        entry = {
            'template': 'tpl',
            'message': 'msg',
            'logger': 'lg',
            'level': 'WARNING',
            'count': 3,
            'first_seen': 'f',
            'last_seen': 'l',
            'context': ['c1'],
            '_internal': 'should-not-leak',
        }
        payload = build_diagnostics_payload(
            instance_id='x',
            service_flags={},
            total_books=None,
            snapshot={'entries': [entry], 'window_start': None, 'taken_at': None, 'dropped': 0},
        )
        self.assertEqual(len(payload['warnings']), 1)
        w = payload['warnings'][0]
        self.assertNotIn('_internal', w)
        self.assertEqual(w['template'], 'tpl')
        self.assertEqual(w['count'], 3)

    def test_total_books_none_allowed(self):
        payload = build_diagnostics_payload('id', {}, None, {'entries': []})
        self.assertIsNone(payload['total_books'])


# ---------------------------------------------------------------------------
# maybe_send_diagnostics — guard clauses
# ---------------------------------------------------------------------------

class TestMaybeSendGuards(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._data_dir = self._tmp.name
        import src.services.diagnostics as _mod
        self._saved_handler = _mod._diagnostics_handler
        self.handler = DiagnosticsLogHandler(data_dir=self._data_dir)
        _mod._diagnostics_handler = self.handler
        self.db = FakeDatabaseService()

        # Record env state
        self._orig_optin = os.environ.pop('DIAGNOSTICS_OPT_IN', None)
        self._orig_endpoint = os.environ.pop('DIAGNOSTICS_ENDPOINT_URL', None)
        self._orig_last_sent = os.environ.pop('DIAGNOSTICS_LAST_SENT', None)
        self._orig_instance = os.environ.pop('DIAGNOSTICS_INSTANCE_ID', None)

    def tearDown(self):
        import src.services.diagnostics as _mod
        _mod._diagnostics_handler = self._saved_handler
        for key, val in [
            ('DIAGNOSTICS_OPT_IN', self._orig_optin),
            ('DIAGNOSTICS_ENDPOINT_URL', self._orig_endpoint),
            ('DIAGNOSTICS_LAST_SENT', self._orig_last_sent),
            ('DIAGNOSTICS_INSTANCE_ID', self._orig_instance),
        ]:
            if val is not None:
                os.environ[key] = val
            else:
                os.environ.pop(key, None)
        self._tmp.cleanup()

    @patch('src.services.diagnostics.requests.post')
    def test_opt_out_no_post(self, mock_post):
        os.environ['DIAGNOSTICS_OPT_IN'] = 'false'
        result = maybe_send_diagnostics(self.db)
        self.assertFalse(result['sent'])
        self.assertEqual(result['reason'], 'opt_out')
        mock_post.assert_not_called()

    @patch('src.services.diagnostics.requests.post')
    def test_no_endpoint_no_post(self, mock_post):
        os.environ['DIAGNOSTICS_OPT_IN'] = 'true'
        os.environ['DIAGNOSTICS_ENDPOINT_URL'] = ''
        result = maybe_send_diagnostics(self.db)
        self.assertFalse(result['sent'])
        self.assertEqual(result['reason'], 'no_endpoint')
        mock_post.assert_not_called()

    @patch('src.services.diagnostics.requests.post')
    def test_last_sent_1h_ago_too_soon(self, mock_post):
        os.environ['DIAGNOSTICS_OPT_IN'] = 'true'
        os.environ['DIAGNOSTICS_ENDPOINT_URL'] = 'http://collector.example.com'
        recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        os.environ['DIAGNOSTICS_LAST_SENT'] = recent
        result = maybe_send_diagnostics(self.db)
        self.assertFalse(result['sent'])
        self.assertEqual(result['reason'], 'too_soon')
        mock_post.assert_not_called()

    @patch('src.services.diagnostics.requests.post')
    def test_last_sent_25h_ago_posts(self, mock_post):
        os.environ['DIAGNOSTICS_OPT_IN'] = 'true'
        os.environ['DIAGNOSTICS_ENDPOINT_URL'] = 'http://collector.example.com'
        old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        os.environ['DIAGNOSTICS_LAST_SENT'] = old
        mock_resp = Mock()
        mock_resp.status_code = 200
        mock_post.return_value = mock_resp
        result = maybe_send_diagnostics(self.db)
        self.assertTrue(result['sent'])
        mock_post.assert_called_once()

    @patch('src.services.diagnostics.requests.post')
    def test_force_bypasses_too_soon(self, mock_post):
        os.environ['DIAGNOSTICS_OPT_IN'] = 'true'
        os.environ['DIAGNOSTICS_ENDPOINT_URL'] = 'http://collector.example.com'
        recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        os.environ['DIAGNOSTICS_LAST_SENT'] = recent
        mock_resp = Mock()
        mock_resp.status_code = 200
        mock_post.return_value = mock_resp
        result = maybe_send_diagnostics(self.db, force=True)
        self.assertTrue(result['sent'])
        mock_post.assert_called_once()

    def test_no_handler(self):
        import src.services.diagnostics as _mod
        _mod._diagnostics_handler = None
        os.environ['DIAGNOSTICS_OPT_IN'] = 'true'
        os.environ['DIAGNOSTICS_ENDPOINT_URL'] = 'http://collector.example.com'
        result = maybe_send_diagnostics(self.db)
        self.assertFalse(result['sent'])
        self.assertEqual(result['reason'], 'no_handler')


# ---------------------------------------------------------------------------
# maybe_send_diagnostics — success and failure paths
# ---------------------------------------------------------------------------

class TestMaybeSendPaths(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._data_dir = self._tmp.name
        import src.services.diagnostics as _mod
        self._saved_handler = _mod._diagnostics_handler
        self.handler = DiagnosticsLogHandler(data_dir=self._data_dir)
        _mod._diagnostics_handler = self.handler
        self.db = FakeDatabaseService()

        self._orig_optin = os.environ.pop('DIAGNOSTICS_OPT_IN', None)
        self._orig_endpoint = os.environ.pop('DIAGNOSTICS_ENDPOINT_URL', None)
        self._orig_last_sent = os.environ.pop('DIAGNOSTICS_LAST_SENT', None)
        self._orig_instance = os.environ.pop('DIAGNOSTICS_INSTANCE_ID', None)
        self._orig_ingest_token = os.environ.pop('DIAGNOSTICS_INGEST_TOKEN', None)

        os.environ['DIAGNOSTICS_OPT_IN'] = 'true'
        os.environ['DIAGNOSTICS_ENDPOINT_URL'] = 'http://collector.example.com'
        os.environ['DIAGNOSTICS_INSTANCE_ID'] = 'test-inst-id'

        # Feed a warning into the handler
        self.handler.emit(_make_record('test', logging.WARNING, 'boom #1'))

    def tearDown(self):
        import src.services.diagnostics as _mod
        _mod._diagnostics_handler = self._saved_handler
        for key, val in [
            ('DIAGNOSTICS_OPT_IN', self._orig_optin),
            ('DIAGNOSTICS_ENDPOINT_URL', self._orig_endpoint),
            ('DIAGNOSTICS_LAST_SENT', self._orig_last_sent),
            ('DIAGNOSTICS_INSTANCE_ID', self._orig_instance),
            ('DIAGNOSTICS_INGEST_TOKEN', self._orig_ingest_token),
        ]:
            if val is not None:
                os.environ[key] = val
            else:
                os.environ.pop(key, None)
        self._tmp.cleanup()

    @patch('src.services.diagnostics.requests.post')
    def test_success_clears_entries_and_sets_last_sent(self, mock_post):
        mock_resp = Mock()
        mock_resp.status_code = 200
        mock_post.return_value = mock_resp

        result = maybe_send_diagnostics(self.db)
        self.assertTrue(result['sent'])
        self.assertEqual(result['reason'], 'ok')
        self.assertGreater(result['warning_count'], 0)

        # Handler entries should be cleared
        with self.handler._lock:
            self.assertEqual(len(self.handler._entries), 0)

        # LAST_SENT env and DB set
        self.assertIn('DIAGNOSTICS_LAST_SENT', os.environ)
        self.assertTrue(any(k == 'DIAGNOSTICS_LAST_SENT' for k, _ in self.db.set_setting_calls))

    @patch('src.services.diagnostics.requests.post')
    def test_http_500_does_not_clear_entries(self, mock_post):
        mock_resp = Mock()
        mock_resp.status_code = 500
        mock_post.return_value = mock_resp

        result = maybe_send_diagnostics(self.db)
        self.assertFalse(result['sent'])
        self.assertEqual(result['reason'], 'http_500')

        with self.handler._lock:
            self.assertGreater(len(self.handler._entries), 0)

        self.assertNotIn('DIAGNOSTICS_LAST_SENT', os.environ)
        self.assertEqual(len(self.db.set_setting_calls), 0)

    @patch('src.services.diagnostics.requests.post', side_effect=ConnectionError('net'))
    def test_exception_does_not_clear_entries(self, mock_post):
        result = maybe_send_diagnostics(self.db)
        self.assertFalse(result['sent'])
        self.assertEqual(result['reason'], 'exception')

        with self.handler._lock:
            self.assertGreater(len(self.handler._entries), 0)

        self.assertNotIn('DIAGNOSTICS_LAST_SENT', os.environ)
        self.assertEqual(len(self.db.set_setting_calls), 0)

    @patch('src.services.diagnostics.requests.post')
    def test_empty_warnings_heartbeat_sends(self, mock_post):
        """An opted-in instance with no entries still sends a metadata heartbeat."""
        import src.services.diagnostics as _mod
        with tempfile.TemporaryDirectory() as empty_dir:
            _mod._diagnostics_handler = DiagnosticsLogHandler(data_dir=empty_dir)

            mock_resp = Mock()
            mock_resp.status_code = 200
            mock_post.return_value = mock_resp

            result = maybe_send_diagnostics(self.db)
            self.assertTrue(result['sent'])
            self.assertEqual(result['warning_count'], 0)
            # Verify payload was posted
            call_kwargs = mock_post.call_args
            payload = call_kwargs.kwargs.get('json') or call_kwargs[1].get('json')
            self.assertEqual(payload['warnings'], [])

    @patch('src.services.diagnostics.requests.post')
    def test_ingest_token_sends_bearer_header(self, mock_post):
        os.environ['DIAGNOSTICS_INGEST_TOKEN'] = 'my-token-abc'
        mock_resp = Mock()
        mock_resp.status_code = 200
        mock_post.return_value = mock_resp

        maybe_send_diagnostics(self.db, force=True)
        call_kwargs = mock_post.call_args
        headers = call_kwargs.kwargs.get('headers') or call_kwargs[1].get('headers', {})
        self.assertEqual(headers.get('Authorization'), 'Bearer my-token-abc')

    @patch('src.services.diagnostics.requests.post')
    def test_no_ingest_token_omits_auth_header(self, mock_post):
        os.environ.pop('DIAGNOSTICS_INGEST_TOKEN', None)
        mock_resp = Mock()
        mock_resp.status_code = 200
        mock_post.return_value = mock_resp

        maybe_send_diagnostics(self.db, force=True)
        call_kwargs = mock_post.call_args
        headers = call_kwargs.kwargs.get('headers') or call_kwargs[1].get('headers', {})
        self.assertNotIn('Authorization', headers)

    @patch('src.services.diagnostics.requests.post')
    def test_token_returned_in_response_persisted(self, mock_post):
        mock_resp = Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {'ok': True, 'token': 'newtok123'}
        mock_post.return_value = mock_resp

        result = maybe_send_diagnostics(self.db, force=True)
        self.assertTrue(result['sent'])
        self.assertEqual(os.environ.get('DIAGNOSTICS_INGEST_TOKEN'), 'newtok123')
        self.assertTrue(any(
            k == 'DIAGNOSTICS_INGEST_TOKEN' and v == 'newtok123'
            for k, v in self.db.set_setting_calls
        ))

    @patch('src.services.diagnostics.requests.post')
    def test_json_parse_error_on_success_does_not_break_send(self, mock_post):
        mock_resp = Mock()
        mock_resp.status_code = 200
        mock_resp.json.side_effect = ValueError("no json")
        mock_post.return_value = mock_resp

        result = maybe_send_diagnostics(self.db, force=True)
        self.assertTrue(result['sent'])
        self.assertNotIn('DIAGNOSTICS_INGEST_TOKEN', os.environ)


# ---------------------------------------------------------------------------
# Route test using MockContainer pattern
# ---------------------------------------------------------------------------

class TestDiagnosticsSendNowRoute(unittest.TestCase):
    """Route test for POST /api/diagnostics/send-now."""

    def setUp(self):
        self._orig = os.environ.pop('DIAGNOSTICS_OPT_IN', None)

    def tearDown(self):
        if self._orig is not None:
            os.environ['DIAGNOSTICS_OPT_IN'] = self._orig
        else:
            os.environ.pop('DIAGNOSTICS_OPT_IN', None)

    @patch('src.services.diagnostics.maybe_send_diagnostics',
           return_value={'sent': True, 'reason': 'ok', 'warning_count': 0})
    def test_send_now_returns_200(self, mock_send):
        from tests.test_webserver import MockContainer
        from src.web_server import create_app

        import src.db.migration_utils
        orig_init = src.db.migration_utils.initialize_database
        mock_db = Mock()
        mock_db.get_all_settings.return_value = {}
        src.db.migration_utils.initialize_database = lambda data_dir: mock_db

        try:
            container = MockContainer()
            app, _ = create_app(test_container=container)
            app.config['TESTING'] = True
            client = app.test_client()

            resp = client.post('/api/diagnostics/send-now')
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertTrue(data['sent'])
            mock_send.assert_called_once()
            _, kwargs = mock_send.call_args
            self.assertTrue(kwargs.get('force', False))
        finally:
            src.db.migration_utils.initialize_database = orig_init


if __name__ == '__main__':
    unittest.main()
