"""Tests for the diagnostics sender (Phase 2: payload builder, daily sender, admin endpoint)."""
import json
import logging
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock, Mock

from src.services.diagnostics import (
    DiagnosticsLogHandler,
    ensure_instance_id,
    build_diagnostics_payload,
    build_title_pattern,
    collect_env_facts,
    maybe_send_diagnostics,
    scrub_diagnostic_text,
    scrub_known_titles,
    _make_template,
    _resolve_max_payload_bytes,
    _sha1_prefix,
    _shed_to_budget,
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


class _FakeBook:
    """Just the title columns the known-title sweep reads from a Book row."""

    def __init__(self, abs_title=None, audio_title=None):
        self.abs_title = abs_title
        self.audio_title = audio_title


class FakeDatabaseService:
    """Records set_setting calls without touching a real database."""

    def __init__(self):
        self.settings: dict = {}
        self.set_setting_calls: list = []
        self.books: list = []

    def set_setting(self, key: str, value: str) -> None:
        self.settings[key] = value
        self.set_setting_calls.append((key, value))

    def get_books_by_status(self, status: str):
        return []

    def get_all_books(self, user_id=None):
        return list(self.books)


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
        self.assertNotIn('manual', payload)
        self.assertNotIn('user_message', payload)

    def test_manual_payload_includes_user_message(self):
        payload = build_diagnostics_payload(
            'abc', {}, 0, {'entries': []},
            manual=True, user_message='The sync button stopped working.',
        )
        self.assertTrue(payload['manual'])
        self.assertEqual(
            payload['user_message'],
            'The sync button stopped working.',
        )

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

    def test_env_facts_present_and_typed(self):
        payload = build_diagnostics_payload('id', {}, None, {'entries': []})
        self.assertIn('env', payload)
        self.assertIn('python', payload['env'])
        self.assertIn('platform', payload['env'])
        self.assertIsInstance(payload['env']['python'], str)
        self.assertIsInstance(payload['env']['platform'], str)

    def test_traceback_field_passes_through_when_present(self):
        entry = {
            'template': 'tpl', 'message': 'msg', 'logger': 'lg', 'level': 'WARNING',
            'count': 1, 'first_seen': 'f', 'last_seen': 'l', 'context': [],
            'traceback': 'Traceback (most recent call last):\nZeroDivisionError: division by zero',
        }
        payload = build_diagnostics_payload(
            instance_id='x', service_flags={}, total_books=None,
            snapshot={'entries': [entry], 'window_start': None, 'taken_at': None, 'dropped': 0},
        )
        self.assertEqual(payload['warnings'][0]['traceback'], entry['traceback'])

    def test_warnings_without_traceback_have_no_traceback_key(self):
        entry = {
            'template': 'tpl', 'message': 'msg', 'logger': 'lg', 'level': 'WARNING',
            'count': 1, 'first_seen': 'f', 'last_seen': 'l', 'context': [],
        }
        payload = build_diagnostics_payload(
            instance_id='x', service_flags={}, total_books=None,
            snapshot={'entries': [entry], 'window_start': None, 'taken_at': None, 'dropped': 0},
        )
        self.assertNotIn('traceback', payload['warnings'][0])


# ---------------------------------------------------------------------------
# collect_env_facts
# ---------------------------------------------------------------------------

class TestCollectEnvFacts(unittest.TestCase):

    def test_facts_have_expected_types(self):
        facts = collect_env_facts()
        self.assertIsInstance(facts['python'], str)
        self.assertIsInstance(facts['platform'], str)
        self.assertIsInstance(facts['container'], bool)

    def test_journal_override_included_only_when_env_set(self):
        orig = os.environ.pop('DB_JOURNAL_MODE', None)
        try:
            facts = collect_env_facts()
            self.assertNotIn('journal_override', facts)

            os.environ['DB_JOURNAL_MODE'] = 'DELETE'
            facts2 = collect_env_facts()
            self.assertEqual(facts2['journal_override'], 'DELETE')
        finally:
            if orig is None:
                os.environ.pop('DB_JOURNAL_MODE', None)
            else:
                os.environ['DB_JOURNAL_MODE'] = orig


# ---------------------------------------------------------------------------
# _resolve_max_payload_bytes / _shed_to_budget
# ---------------------------------------------------------------------------

class TestResolveMaxPayloadBytes(unittest.TestCase):

    def setUp(self):
        self._orig = os.environ.pop('DIAGNOSTICS_MAX_PAYLOAD_BYTES', None)

    def tearDown(self):
        if self._orig is not None:
            os.environ['DIAGNOSTICS_MAX_PAYLOAD_BYTES'] = self._orig
        else:
            os.environ.pop('DIAGNOSTICS_MAX_PAYLOAD_BYTES', None)

    def test_unset_returns_default(self):
        os.environ.pop('DIAGNOSTICS_MAX_PAYLOAD_BYTES', None)
        self.assertEqual(_resolve_max_payload_bytes(), 800_000)

    def test_invalid_value_returns_default(self):
        os.environ['DIAGNOSTICS_MAX_PAYLOAD_BYTES'] = 'not-a-number'
        self.assertEqual(_resolve_max_payload_bytes(), 800_000)

    def test_zero_is_passed_through_to_disable_shedding(self):
        os.environ['DIAGNOSTICS_MAX_PAYLOAD_BYTES'] = '0'
        self.assertEqual(_resolve_max_payload_bytes(), 0)

    def test_valid_value_is_used(self):
        os.environ['DIAGNOSTICS_MAX_PAYLOAD_BYTES'] = '12345'
        self.assertEqual(_resolve_max_payload_bytes(), 12345)


class TestShedToBudget(unittest.TestCase):
    """Tests for _shed_to_budget as a pure function (change 3)."""

    def _make_entries(self, n: int, bulky: bool = True) -> list:
        entries = []
        for i in range(n):
            entry = {
                'template': f'Template shape number {i}',
                'message': f'Message body for entry {i}',
                'logger': 'src.sync_manager',
                'level': 'WARNING',
                'count': i + 1,  # strictly increasing count
                'first_seen': '2026-01-01T00:00:00+00:00',
                'last_seen': '2026-01-01T00:00:00+00:00',
            }
            if bulky:
                entry['context'] = [f'context line {j} padding text' for j in range(15)]
                entry['traceback'] = 'Traceback (most recent call last):\n' + ('frame line\n' * 8)
            entries.append(entry)
        return entries

    def test_max_bytes_zero_leaves_payload_untouched(self):
        entries = self._make_entries(5)
        payload = {'warnings': entries}
        snapshot = {'_snapshot_key_counts': {}, 'entries': []}
        result = _shed_to_budget(payload, snapshot, 0)
        self.assertIs(result, payload)
        self.assertNotIn('shed', payload)
        self.assertTrue(all('context' in e for e in payload['warnings']))

    def test_already_under_budget_is_untouched(self):
        entries = self._make_entries(2)
        payload = {'warnings': entries}
        snapshot = {'_snapshot_key_counts': {}, 'entries': []}
        full_size = len(json.dumps(payload))
        result = _shed_to_budget(payload, snapshot, full_size + 1000)
        self.assertIs(result, payload)
        self.assertNotIn('shed', payload)

    def test_moderate_overage_strips_context_lowest_count_first(self):
        entries = self._make_entries(30)
        payload = {'warnings': entries}
        snapshot_key_counts = {
            (e['logger'], e['level'], e['template']): e['count'] for e in entries
        }
        snapshot = {
            '_snapshot_key_counts': snapshot_key_counts,
            'entries': [dict(e) for e in entries],
        }
        full_size = len(json.dumps(payload))
        # Comfortably below the full size, but not so small it forces whole
        # low-count entries to be dropped outright (pass 2).
        budget = int(full_size * 0.6)

        result = _shed_to_budget(payload, snapshot, budget)

        self.assertLessEqual(len(json.dumps(result)), budget)
        self.assertEqual(len(result['warnings']), 30, "no whole entries should be dropped at this budget")
        stripped = [e for e in result['warnings'] if 'context' not in e]
        kept = [e for e in result['warnings'] if 'context' in e]
        self.assertTrue(stripped, "expected at least one entry to lose context")
        if kept:
            self.assertLessEqual(
                max(e['count'] for e in stripped), min(e['count'] for e in kept),
                "lowest-count entries must lose context before higher-count ones",
            )
        self.assertIn('shed', result)
        self.assertGreater(result['shed']['context_stripped'], 0)
        self.assertEqual(result['shed']['entries_deferred'], 0)

    def test_severe_overage_defers_whole_entries_out_of_snapshot(self):
        entries = self._make_entries(30)
        payload = {'warnings': entries}
        snapshot_key_counts = {
            (e['logger'], e['level'], e['template']): e['count'] for e in entries
        }
        snapshot = {
            '_snapshot_key_counts': snapshot_key_counts,
            'entries': [dict(e) for e in entries],
        }
        budget = 900  # small enough to force pass 2 even after stripping everything

        result = _shed_to_budget(payload, snapshot, budget)

        self.assertLessEqual(len(json.dumps(result)), budget)
        self.assertLess(len(result['warnings']), 30)
        self.assertIn('shed', result)
        self.assertGreater(result['shed']['entries_deferred'], 0)

        remaining_keys = {
            (e['logger'], e['level'], e['template']) for e in result['warnings']
        }
        original_keys = {
            (e['logger'], e['level'], e['template']) for e in entries
        }
        deferred_keys = original_keys - remaining_keys
        self.assertTrue(deferred_keys)

        # Deferred entries must be gone from BOTH the snapshot key-count
        # index and the snapshot's entries list, so clear_snapshot() will
        # leave them buffered in the live handler.
        for key in deferred_keys:
            self.assertNotIn(key, snapshot['_snapshot_key_counts'])
            self.assertFalse(any(
                (e['logger'], e['level'], e['template']) == key
                for e in snapshot['entries']
            ))

        # Deferral is ascending-count-first.
        deferred_counts = [
            e['count'] for e in entries
            if (e['logger'], e['level'], e['template']) in deferred_keys
        ]
        remaining_counts = [e['count'] for e in result['warnings']]
        if remaining_counts:
            self.assertLessEqual(max(deferred_counts), min(remaining_counts))


class TestShedToBudgetHandlerRoundTrip(unittest.TestCase):
    """Real-handler round trip: entries deferred by shedding survive clear_snapshot."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._data_dir = self._tmp.name
        os.environ['DIAGNOSTICS_OPT_IN'] = 'true'
        self._test_logger = logging.getLogger('test_shed_roundtrip')
        self._test_logger.propagate = False
        self._test_logger.setLevel(logging.DEBUG)
        self.handler = DiagnosticsLogHandler(data_dir=self._data_dir)
        self.handler.setLevel(logging.INFO)
        self._test_logger.addHandler(self.handler)

    def tearDown(self):
        self._test_logger.handlers.clear()
        os.environ.pop('DIAGNOSTICS_OPT_IN', None)
        self._tmp.cleanup()

    def test_deferred_entries_remain_buffered_after_clear_snapshot(self):
        import string

        for i, letter in enumerate(string.ascii_lowercase):
            for _ in range(i + 1):
                self._test_logger.warning("Distinct warning shape %s", letter)

        snap = self.handler.snapshot()
        payload = build_diagnostics_payload('inst', {}, None, snap)
        total_before = len(payload['warnings'])
        self.assertEqual(total_before, 26)

        shed_payload = _shed_to_budget(payload, snap, 900)
        remaining_keys = {
            (e['logger'], e['level'], e['template']) for e in shed_payload['warnings']
        }
        self.assertLess(len(remaining_keys), total_before)

        with self.handler._lock:
            keys_before_clear = set(self.handler._entries.keys())
        deferred_keys = keys_before_clear - remaining_keys
        self.assertTrue(deferred_keys)

        self.handler.clear_snapshot(snap)

        with self.handler._lock:
            keys_after_clear = set(self.handler._entries.keys())

        for key in deferred_keys:
            self.assertIn(
                key, keys_after_clear,
                f"deferred key {key} should survive clear_snapshot",
            )
        for key in remaining_keys:
            self.assertNotIn(
                key, keys_after_clear,
                f"successfully-sent key {key} should be cleared",
            )


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
    def test_manual_success_returns_submission_without_updating_last_sent(self, mock_post):
        mock_resp = Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            'ok': True,
            'batch_id': 42,
            'token': 'manual-token',
        }
        mock_post.return_value = mock_resp

        result = maybe_send_diagnostics(
            self.db,
            force=True,
            manual=True,
            user_message='It stopped at 50%.',
        )

        self.assertTrue(result['sent'])
        self.assertEqual(result['submission_id'], 42)
        self.assertNotIn('DIAGNOSTICS_LAST_SENT', os.environ)
        self.assertFalse(any(
            key == 'DIAGNOSTICS_LAST_SENT'
            for key, _value in self.db.set_setting_calls
        ))
        self.assertEqual(os.environ['DIAGNOSTICS_INGEST_TOKEN'], 'manual-token')
        with self.handler._lock:
            self.assertEqual(len(self.handler._entries), 0)

        payload = mock_post.call_args.kwargs['json']
        self.assertTrue(payload['manual'])
        self.assertEqual(payload['user_message'], 'It stopped at 50%.')

    @patch('src.services.diagnostics.requests.post')
    def test_comment_only_manual_report_includes_recent_logs(self, mock_post):
        """Report #631: a manual comment must not arrive without its recent logs."""
        mock_resp = Mock(status_code=200)
        mock_resp.json.return_value = {'ok': True, 'batch_id': 631}
        mock_post.return_value = mock_resp

        with self.handler._lock:
            self.handler._entries.clear()
            self.handler._ring.clear()
        self.handler.emit(_make_record(
            'src.sync_manager',
            logging.INFO,
            'Preparing ABS audio files for transcription' + ('x' * 500),
        ))

        result = maybe_send_diagnostics(
            self.db,
            force=True,
            manual=True,
            user_message="BookBridge can't download ABS audio files to transcript",
        )

        payload = mock_post.call_args.kwargs['json']
        self.assertTrue(result['sent'])
        self.assertEqual(payload['warnings'], [])
        self.assertEqual(len(payload['recent_logs']), 1)
        self.assertEqual(len(payload['recent_logs'][0]), 400)
        self.assertIn(
            'Preparing ABS audio files for transcription',
            payload['recent_logs'][0],
        )

    @patch('src.services.diagnostics.requests.post')
    def test_concurrent_sends_serialize_snapshot_post_and_clear(self, mock_post):
        first_post_started = threading.Event()
        release_first_post = threading.Event()
        second_post_started = threading.Event()
        payloads = []

        def post_side_effect(*_args, **kwargs):
            payloads.append(kwargs['json'])
            if len(payloads) == 1:
                first_post_started.set()
                self.assertTrue(release_first_post.wait(2))
            else:
                second_post_started.set()
            response = Mock(status_code=200)
            response.json.return_value = {'ok': True}
            return response

        mock_post.side_effect = post_side_effect
        results = []

        def send() -> None:
            results.append(maybe_send_diagnostics(
                self.db, force=True, manual=True,
            ))

        first = threading.Thread(target=send)
        second = threading.Thread(target=send)
        first.start()
        self.assertTrue(first_post_started.wait(2))
        second.start()
        self.assertFalse(second_post_started.wait(0.1))

        self.handler.emit(_make_record('test', logging.WARNING, 'boom #2'))
        release_first_post.set()
        first.join(2)
        second.join(2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(len(results), 2)
        self.assertTrue(all(result['sent'] for result in results))
        self.assertEqual(
            [payload['warnings'][0]['count'] for payload in payloads],
            [1, 1],
        )

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

    @patch('src.services.diagnostics.requests.post')
    def test_http_429_preserves_receiver_error_and_retry(self, mock_post):
        mock_resp = Mock()
        mock_resp.status_code = 429
        mock_resp.json.return_value = {
            'error': 'manual_report_quota_exceeded',
            'retry_after_hours': 3.5,
        }
        mock_post.return_value = mock_resp

        result = maybe_send_diagnostics(
            self.db,
            force=True,
            manual=True,
        )

        self.assertFalse(result['sent'])
        self.assertEqual(result['reason'], 'http_429')
        self.assertEqual(result['error'], 'manual_report_quota_exceeded')
        self.assertEqual(result['retry_after_hours'], 3.5)
        with self.handler._lock:
            self.assertGreater(len(self.handler._entries), 0)

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

    @patch('src.services.diagnostics.requests.post')
    def test_max_payload_bytes_env_sheds_oversized_payload(self, mock_post):
        """DIAGNOSTICS_MAX_PAYLOAD_BYTES forces _maybe_send to shed before posting."""
        for i in range(30):
            self.handler.emit(_make_record(
                f'test.shed.{i}', logging.WARNING,
                f'Oversized payload warning shape {i} ' + ('z' * 200),
            ))

        orig_budget = os.environ.pop('DIAGNOSTICS_MAX_PAYLOAD_BYTES', None)
        os.environ['DIAGNOSTICS_MAX_PAYLOAD_BYTES'] = '2000'
        try:
            mock_resp = Mock()
            mock_resp.status_code = 200
            mock_post.return_value = mock_resp

            result = maybe_send_diagnostics(self.db)
            self.assertTrue(result['sent'])

            posted_payload = mock_post.call_args.kwargs['json']
            posted_size = len(json.dumps(posted_payload))
            self.assertLessEqual(posted_size, 2000)
            self.assertIn('shed', posted_payload)
        finally:
            if orig_budget is None:
                os.environ.pop('DIAGNOSTICS_MAX_PAYLOAD_BYTES', None)
            else:
                os.environ['DIAGNOSTICS_MAX_PAYLOAD_BYTES'] = orig_budget


# ---------------------------------------------------------------------------
# Known-title scrubbing: build_title_pattern / scrub_known_titles
# ---------------------------------------------------------------------------

class TestKnownTitleScrub(unittest.TestCase):

    def test_bare_title_gets_the_quoted_span_token(self):
        pattern = build_title_pattern(['The Great Gatsby'])
        token = f"t:{_sha1_prefix('The Great Gatsby')}"
        self.assertEqual(
            scrub_known_titles('❌ The Great Gatsby: ConnectionError', pattern),
            f'❌ {token}: ConnectionError',
        )
        # Same token the quoted-span rule mints for the same text.
        self.assertIn(
            token,
            scrub_diagnostic_text("Hardcover: No match found for 'The Great Gatsby'"),
        )

    def test_match_is_case_insensitive_and_whitespace_tolerant(self):
        pattern = build_title_pattern(['  The   Great Gatsby '])
        out = scrub_known_titles('read THE GREAT GATSBY and the great  gatsby', pattern)
        self.assertNotIn('gatsby', out.lower())
        self.assertEqual(out.count('t:'), 2)

    def test_longer_title_replaced_whole_over_the_shorter_it_contains(self):
        pattern = build_title_pattern(['Dune', 'Dune Messiah'])
        self.assertEqual(
            scrub_known_titles('failed on Dune Messiah, then on Dune', pattern),
            f"failed on t:{_sha1_prefix('Dune Messiah')}, then on t:{_sha1_prefix('Dune')}",
        )

    def test_short_titles_left_alone(self):
        self.assertIsNone(build_title_pattern(['It', 'Us', 'Her']))
        pattern = build_title_pattern(['It', 'Us', 'Her', 'Dune'])
        self.assertEqual(
            scrub_known_titles('It is us and her, not Dune', pattern),
            f"It is us and her, not t:{_sha1_prefix('Dune')}",
        )

    def test_bare_number_titles_and_non_strings_are_skipped(self):
        self.assertIsNone(build_title_pattern(['1984', '4 3 2 1', '', None, 7]))

    def test_regex_metacharacters_in_titles_are_literal(self):
        pattern = build_title_pattern(['C++ for Kids (2nd ed.)', 'What If?', '[REC]'])
        out = scrub_known_titles(
            'C++ for Kids (2nd ed.) broke; C++ for Kids (2nd edX) did not; '
            'What If? vs What Ifx; [REC] rec',
            pattern,
        )
        self.assertEqual(
            out,
            f"t:{_sha1_prefix('C++ for Kids (2nd ed.)')} broke; C++ for Kids (2nd edX) did not; "
            f"t:{_sha1_prefix('What If?')} vs What Ifx; t:{_sha1_prefix('[REC]')} rec",
        )

    def test_word_boundaries_hold_around_punctuation(self):
        pattern = build_title_pattern(['Dune', "'Salem's Lot"])
        dune = _sha1_prefix('Dune')
        salems = _sha1_prefix("'Salem's Lot")
        self.assertEqual(
            scrub_known_titles("Dunes, redune, (Dune) and 'Salem's Lot.", pattern),
            f"Dunes, redune, (t:{dune}) and t:{salems}.",
        )

    def test_digit_collapsed_variant_covers_the_template_form(self):
        pattern = build_title_pattern(['Catch-22'])
        template = _make_template('Sync failed for Catch-22 now')
        self.assertEqual(template, 'Sync failed for Catch-# now')
        self.assertEqual(
            scrub_known_titles(template, pattern),
            f"Sync failed for t:{_sha1_prefix('Catch-#')} now",
        )

    def test_none_pattern_and_empty_text_pass_through(self):
        self.assertEqual(scrub_known_titles('as is', None), 'as is')
        self.assertEqual(scrub_known_titles('', build_title_pattern(['Dune'])), '')

    def test_library_of_thousands_builds_one_pattern(self):
        titles = [
            f'Shadow of the {word} {i}'
            for i, word in enumerate(['Tower', 'River', 'Crown'] * 1700)
        ]
        titles.append('Word ' * 100)  # as long as a Book title column allows
        pattern = build_title_pattern(titles)
        self.assertEqual(
            scrub_known_titles('x Shadow of the River 4999 y', pattern),
            f"x t:{_sha1_prefix('Shadow of the River 4999')} y",
        )
        self.assertEqual(
            scrub_known_titles('a ' + 'Word ' * 100 + 'b', pattern),
            f"a t:{_sha1_prefix(('Word ' * 100).strip())} b",
        )


# ---------------------------------------------------------------------------
# maybe_send_diagnostics: known-title sweep over the posted report
# ---------------------------------------------------------------------------

class TestMaybeSendScrubsKnownTitles(unittest.TestCase):

    TITLE = 'The Great Gatsby'

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        import src.services.diagnostics as _mod
        self._saved_handler = _mod._diagnostics_handler
        self.handler = DiagnosticsLogHandler(data_dir=self._tmp.name)
        _mod._diagnostics_handler = self.handler
        self.db = FakeDatabaseService()
        self.db.books = [
            _FakeBook(abs_title=self.TITLE),
            _FakeBook(audio_title='Dune Messiah'),
        ]

        self._saved_env = {
            key: os.environ.pop(key, None)
            for key in (
                'DIAGNOSTICS_OPT_IN', 'DIAGNOSTICS_ENDPOINT_URL',
                'DIAGNOSTICS_LAST_SENT', 'DIAGNOSTICS_INSTANCE_ID',
                'DIAGNOSTICS_INGEST_TOKEN', 'DIAGNOSTICS_MAX_PAYLOAD_BYTES',
            )
        }
        os.environ['DIAGNOSTICS_OPT_IN'] = 'true'
        os.environ['DIAGNOSTICS_ENDPOINT_URL'] = 'http://collector.example.com'
        os.environ['DIAGNOSTICS_INSTANCE_ID'] = 'test-inst-id'

    def tearDown(self):
        import src.services.diagnostics as _mod
        _mod._diagnostics_handler = self._saved_handler
        for key, val in self._saved_env.items():
            if val is not None:
                os.environ[key] = val
            else:
                os.environ.pop(key, None)
        self._tmp.cleanup()

    def _post(self, mock_post, **kwargs):
        mock_resp = Mock(status_code=200)
        mock_resp.json.return_value = {'ok': True}
        mock_post.return_value = mock_resp
        result = maybe_send_diagnostics(self.db, force=True, **kwargs)
        self.assertTrue(result['sent'])
        return mock_post.call_args.kwargs['json']

    @patch('src.services.diagnostics.requests.post')
    def test_bare_title_swept_from_message_context_template_and_recent_logs(self, mock_post):
        self.handler.emit(_make_record(
            'src.sync_manager', logging.INFO,
            f'Preparing {self.TITLE} for transcription',
        ))
        self.handler.emit(_make_record(
            'src.sync_manager', logging.ERROR,
            f'❌ {self.TITLE}: ConnectionError',
        ))

        payload = self._post(mock_post, manual=True, user_message='it broke')

        token = f"t:{_sha1_prefix(self.TITLE)}"
        (warning,) = payload['warnings']
        self.assertEqual(warning['message'], f'❌ {token}: ConnectionError')
        self.assertEqual(warning['template'], '❌ t:#: ConnectionError')
        self.assertTrue(any(token in line for line in warning['context']))
        self.assertTrue(any(token in line for line in payload['recent_logs']))
        self.assertNotIn(self.TITLE, json.dumps(payload, ensure_ascii=False))

    @patch('src.services.diagnostics.requests.post')
    def test_traceback_tail_is_swept(self, mock_post):
        record = _make_record('src.sync_manager', logging.ERROR, 'alignment failed')
        try:
            raise RuntimeError(f'no audio for {self.TITLE}')
        except RuntimeError:
            record.exc_info = sys.exc_info()
        self.handler.emit(record)

        payload = self._post(mock_post)

        (warning,) = payload['warnings']
        self.assertIn('RuntimeError', warning['traceback'])
        self.assertIn(f"t:{_sha1_prefix(self.TITLE)}", warning['traceback'])
        self.assertNotIn(self.TITLE, warning['traceback'])

    @patch('src.services.diagnostics.requests.post')
    def test_title_loading_failure_still_posts_the_otherwise_scrubbed_report(self, mock_post):
        def _boom(user_id=None):
            raise RuntimeError('database is locked')
        self.db.get_all_books = _boom
        self.handler.emit(_make_record(
            'src.sync_manager', logging.WARNING,
            f'Could not notify reader@example.com about {self.TITLE}',
        ))

        payload = self._post(mock_post)

        (warning,) = payload['warnings']
        self.assertIn('email:', warning['message'])
        self.assertNotIn('reader@example.com', warning['message'])

    @patch('src.services.diagnostics.requests.post')
    def test_database_stand_in_without_get_all_books_is_tolerated(self, mock_post):
        class _SettingsOnly:
            def set_setting(self, key, value):
                pass

        self.db = _SettingsOnly()
        self.handler.emit(_make_record('test', logging.WARNING, 'boom'))

        payload = self._post(mock_post)

        self.assertEqual(len(payload['warnings']), 1)

    @patch('src.services.diagnostics.requests.post')
    def test_shedding_still_defers_by_live_key_when_titles_are_swept(self, mock_post):
        """The template is the key _shed_to_budget hands back to clear_snapshot.

        If titles were swept out of templates before shedding, deferred
        entries would no longer match their live keys and clear_snapshot
        would drop them unsent.
        """
        for i in range(30):
            self.handler.emit(_make_record(
                f'test.shed.{i}', logging.WARNING,
                f'Oversized shape {i} for {self.TITLE} ' + ('z' * 200),
            ))
        os.environ['DIAGNOSTICS_MAX_PAYLOAD_BYTES'] = '9000'

        payload = self._post(mock_post)

        self.assertIn('shed', payload)
        self.assertLessEqual(len(json.dumps(payload)), 9000)
        self.assertNotIn(self.TITLE, json.dumps(payload, ensure_ascii=False))
        sent = len(payload['warnings'])
        self.assertTrue(0 < sent < 30, sent)
        with self.handler._lock:
            remaining = len(self.handler._entries)
        self.assertEqual(remaining, 30 - sent)


# ---------------------------------------------------------------------------
# Route test using MockContainer pattern
# ---------------------------------------------------------------------------

class TestDiagnosticsSendNowRoute(unittest.TestCase):
    """Route test for POST /api/diagnostics/send-now."""

    def setUp(self):
        self._orig = os.environ.pop('DIAGNOSTICS_OPT_IN', None)
        from tests.test_webserver import MockContainer
        from src.web_server import create_app

        import src.db.migration_utils
        self._orig_init = src.db.migration_utils.initialize_database
        mock_db = Mock()
        mock_db.get_all_settings.return_value = {}
        src.db.migration_utils.initialize_database = lambda data_dir: mock_db

        container = MockContainer()
        self.app, _ = create_app(test_container=container)
        self.app.config['TESTING'] = True
        self.client = self.app.test_client()

    def tearDown(self):
        import src.db.migration_utils
        src.db.migration_utils.initialize_database = self._orig_init
        if self._orig is not None:
            os.environ['DIAGNOSTICS_OPT_IN'] = self._orig
        else:
            os.environ.pop('DIAGNOSTICS_OPT_IN', None)

    @patch('src.services.diagnostics.maybe_send_diagnostics',
           return_value={'sent': True, 'reason': 'ok', 'warning_count': 0,
                         'submission_id': 7})
    def test_send_now_trims_message_and_marks_manual(self, mock_send):
        resp = self.client.post(
            '/api/diagnostics/send-now',
            json={'message': '  Sync stopped  '},
        )

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()['sent'])
        mock_send.assert_called_once()
        _, kwargs = mock_send.call_args
        self.assertTrue(kwargs['force'])
        self.assertTrue(kwargs['manual'])
        self.assertEqual(kwargs['user_message'], 'Sync stopped')

    @patch('src.services.diagnostics.maybe_send_diagnostics',
           return_value={'sent': True, 'reason': 'ok', 'warning_count': 0})
    def test_send_now_allows_missing_message(self, mock_send):
        resp = self.client.post('/api/diagnostics/send-now')

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(mock_send.call_args.kwargs['user_message'], '')

    @patch('src.services.diagnostics.maybe_send_diagnostics')
    def test_send_now_rejects_non_string_message(self, mock_send):
        resp = self.client.post(
            '/api/diagnostics/send-now',
            json={'message': 123},
        )

        self.assertEqual(resp.status_code, 400)
        mock_send.assert_not_called()

    @patch('src.services.diagnostics.maybe_send_diagnostics')
    def test_send_now_rejects_message_over_2000_characters(self, mock_send):
        resp = self.client.post(
            '/api/diagnostics/send-now',
            json={'message': 'x' * 2001},
        )

        self.assertEqual(resp.status_code, 400)
        mock_send.assert_not_called()

    @patch('src.services.diagnostics.maybe_send_diagnostics', return_value={
        'sent': False,
        'reason': 'http_429',
        'warning_count': 1,
        'error': 'manual_report_quota_exceeded',
        'retry_after_hours': 2,
    })
    def test_send_now_preserves_429_status_and_details(self, _mock_send):
        resp = self.client.post('/api/diagnostics/send-now', json={})

        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.get_json()['retry_after_hours'], 2)


if __name__ == '__main__':
    unittest.main()
