#!/usr/bin/env python3
"""CWA Kobo-protocol cooldown after host-level failures (#496)."""

import logging
import sys
import unittest
from email.utils import format_datetime
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import requests

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.api import cwa_sync_api
from src.api.cwa_sync_api import CWASyncApi, COOLDOWN_BASE_SECONDS, COOLDOWN_MAX_SECONDS
from src.utils.logging_utils import get_persistent_condition_logger


def _resp(status=200, headers=None, payload=None):
    r = Mock()
    r.status_code = status
    r.headers = headers or {}
    r.json.return_value = payload if payload is not None else [{
        "CurrentBookmark": {"ProgressPercent": 50.0}, "StatusInfo": {"Status": "Reading"}}]
    return r


class TestCwaCooldown(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict('os.environ', {
            'CWA_SERVER': 'http://cwa.local:8083', 'CWA_SYNC_ENABLED': 'true',
            'CWA_SYNC_TOKEN': 'tok'})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.api = CWASyncApi(cwa_client=Mock(base_url='http://cwa.local:8083'))
        self.api._session = Mock()
        self.now = 1000.0
        mono = patch.object(cwa_sync_api.time, 'monotonic', side_effect=lambda: self.now)
        mono.start()
        self.addCleanup(mono.stop)
        get_persistent_condition_logger()._counts.pop(self.api._cooldown_key, None)

    def _poll(self, n=3):
        return [self.api.get_reading_state(f"u{i}") for i in range(n)]

    def test_timeout_on_first_book_skips_rest_of_poll(self):
        self.api._session.get.side_effect = requests.Timeout()
        self.assertEqual(self._poll(), [None, None, None])
        self.assertEqual(self.api._session.get.call_count, 1)

    def test_failed_request_never_logs_the_sync_token(self):
        self.api._token = "s3cret-sync-token"
        self.api._session.get.side_effect = requests.ConnectionError(
            "HTTPConnectionPool(host='cwa.local', port=8083): Max retries exceeded with url: "
            "/kobo/s3cret-sync-token/v1/library/u0/state")
        with self.assertLogs("src.api.cwa_sync_api", level="DEBUG") as logs:
            self.assertIsNone(self.api.get_reading_state("u0"))
        rendered = "\n".join(logs.output) + "".join(
            logging.Formatter().formatException(r.exc_info) for r in logs.records if r.exc_info)
        self.assertIn("<token>", rendered)
        self.assertNotIn("s3cret-sync-token", rendered)

    def test_sync_cycle_calls_send_nothing_during_cooldown(self):
        self.api._session.get.side_effect = requests.Timeout()
        self.api.get_reading_state("u0")
        self.api._session.reset_mock()
        self.assertFalse(self.api.update_reading_state("u1", 0.5))
        self.assertIsNone(self.api.get_reading_state("u1"))
        self.assertIsNone(self.api.get_kepub_download_path("u1"))
        self.assertIsNone(self.api.download_kepub("u1"))
        self.api._session.get.assert_not_called()
        self.api._session.put.assert_not_called()

    def test_connection_error_and_5xx_trigger(self):
        for effect in (requests.ConnectionError(), _resp(502), _resp(503), _resp(504), _resp(500)):
            with self.subTest(effect=effect):
                self.api._cooldown_until = 0.0
                self.api._session.reset_mock()
                self.api._session.get.side_effect = None
                self.api._session.get.return_value = None
                if isinstance(effect, Exception):
                    self.api._session.get.side_effect = effect
                else:
                    self.api._session.get.return_value = effect
                self._poll()
                self.assertEqual(self.api._session.get.call_count, 1)

    def test_put_5xx_triggers_and_returns_false(self):
        self.api._session.put.return_value = _resp(503)
        self.assertFalse(self.api.update_reading_state("u0", 0.5))
        self.api._session.put.reset_mock()
        self.assertFalse(self.api.update_reading_state("u1", 0.5))
        self.api._session.put.assert_not_called()

    def test_404_does_not_trigger(self):
        self.api._session.get.return_value = _resp(404)
        self.assertEqual(self._poll(), [None, None, None])
        self.assertEqual(self.api._session.get.call_count, 3)

    def test_429_retry_after_seconds(self):
        self.api._session.get.return_value = _resp(429, {"Retry-After": "300"})
        self.api.get_reading_state("u0")
        self.now += 299
        self.api.get_reading_state("u1")
        self.assertEqual(self.api._session.get.call_count, 1)
        self.now += 2
        self.api.get_reading_state("u1")
        self.assertEqual(self.api._session.get.call_count, 2)

    def test_429_retry_after_http_date(self):
        when = datetime.now(timezone.utc) + timedelta(seconds=200)
        self.api._session.get.return_value = _resp(429, {"Retry-After": format_datetime(when, usegmt=True)})
        self.api.get_reading_state("u0")
        self.assertTrue(150 < self.api._cooldown_until - self.now <= 200)

    def test_success_after_expiry_resets_backoff_and_resolves(self):
        self.api._session.get.side_effect = requests.Timeout()
        self.api.get_reading_state("u0")
        self.now += COOLDOWN_BASE_SECONDS + 1
        self.api._session.get.side_effect = None
        self.api._session.get.return_value = _resp(200)
        with self.assertLogs(cwa_sync_api.logger, level='INFO') as cm:
            self.assertIsNotNone(self.api.get_reading_state("u0"))
        self.assertTrue(any("server responding again" in m for m in cm.output))
        self.assertEqual(self.api._cooldown_failures, 0)
        self.api._session.get.side_effect = requests.Timeout()
        self.api.get_reading_state("u0")
        self.assertEqual(self.api._cooldown_until - self.now, COOLDOWN_BASE_SECONDS)

    def test_consecutive_failures_double_to_cap(self):
        self.api._session.get.side_effect = requests.Timeout()
        lengths = []
        for _ in range(8):
            self.api.get_reading_state("u0")
            lengths.append(self.api._cooldown_until - self.now)
            self.now = self.api._cooldown_until + 1
        self.assertEqual(lengths[:4], [60, 120, 240, 480])
        self.assertEqual(lengths[-1], COOLDOWN_MAX_SECONDS)

    def test_cooldown_warning_logged_once_per_cooldown(self):
        self.api._session.get.side_effect = requests.Timeout()
        with self.assertLogs(cwa_sync_api.logger, level='WARNING') as cm:
            self._poll(5)
        self.assertEqual(sum("pausing requests" in m for m in cm.output), 1)


if __name__ == '__main__':
    unittest.main()
