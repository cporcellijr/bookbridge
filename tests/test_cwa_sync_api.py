#!/usr/bin/env python3
"""Unit tests for CWASyncApi."""

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.api.cwa_sync_api import CWASyncApi, STATUS_READING, STATUS_FINISHED, STATUS_READY


class TestCWASyncApi(unittest.TestCase):

    def _make_client(self, server='http://cwa.local:8083', token='abc123token',
                     enabled='true', cwa_client=None):
        """Create a client with config snapshotted from env."""
        with patch.dict('os.environ', {
            'CWA_SERVER': server,
            'CWA_SYNC_ENABLED': enabled,
            'CWA_SYNC_TOKEN': token,
        }):
            return CWASyncApi(cwa_client=cwa_client)

    def setUp(self):
        self.mock_cwa_client = Mock()
        self.mock_cwa_client.base_url = 'http://cwa.local:8083'
        self.client = self._make_client(cwa_client=self.mock_cwa_client)

    # -- Configuration --

    def test_is_configured_when_all_set(self):
        self.assertTrue(self.client.is_configured())

    def test_not_configured_when_disabled(self):
        client = self._make_client(enabled='false', cwa_client=self.mock_cwa_client)
        self.assertFalse(client.is_configured())

    def test_not_configured_when_no_token(self):
        client = self._make_client(token='', cwa_client=self.mock_cwa_client)
        self.assertFalse(client.is_configured())

    def test_not_configured_when_no_server(self):
        mock_cwa = Mock()
        mock_cwa.base_url = ''
        client = self._make_client(server='', cwa_client=mock_cwa)
        self.assertFalse(client.is_configured())

    # -- URL construction --

    def test_base_url_construction(self):
        self.assertEqual(
            self.client._base_url,
            'http://cwa.local:8083/kobo/abc123token/v1'
        )

    def test_server_from_cwa_client(self):
        """Server URL should come from injected CWA client's base_url."""
        self.assertEqual(self.client._server, 'http://cwa.local:8083')

    # -- get_reading_state --

    def test_get_reading_state_success(self):
        mock_resp = Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [{
            "EntitlementId": "test-uuid",
            "CurrentBookmark": {
                "ProgressPercent": 53.0,  # 0-100 scale
                "ContentSourceProgressPercent": 53.0,
                "Location": {
                    "Source": "chapter3.html",
                    "Type": "KoboSpan",
                    "Value": "kobo.1.1",
                },
            },
            "StatusInfo": {
                "Status": "Reading",
            },
        }]
        self.client._session = Mock()
        self.client._session.get.return_value = mock_resp

        result = self.client.get_reading_state("test-uuid")

        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["progress_percent"], 0.53)
        self.assertEqual(result["status"], "Reading")
        self.assertEqual(result["href"], "chapter3.html")
        self.assertEqual(result["frag"], "kobo.1.1")

    def test_get_reading_state_empty_response(self):
        mock_resp = Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = []
        self.client._session = Mock()
        self.client._session.get.return_value = mock_resp

        result = self.client.get_reading_state("test-uuid")
        self.assertIsNone(result)

    def test_get_reading_state_http_error(self):
        mock_resp = Mock()
        mock_resp.status_code = 404
        self.client._session = Mock()
        self.client._session.get.return_value = mock_resp

        result = self.client.get_reading_state("test-uuid")
        self.assertIsNone(result)

    # -- update_reading_state --

    def test_update_reading_state_success(self):
        mock_resp = Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"RequestResult": "Success"}
        self.client._session = Mock()
        self.client._session.put.return_value = mock_resp

        result = self.client.update_reading_state("test-uuid", 0.75, STATUS_READING)

        self.assertTrue(result)
        call_args = self.client._session.put.call_args
        payload = call_args[1]['json']
        # Should be converted to 0-100 scale for the API
        self.assertAlmostEqual(
            payload["ReadingStates"][0]["CurrentBookmark"]["ProgressPercent"],
            75.0,
        )
        self.assertEqual(
            payload["ReadingStates"][0]["StatusInfo"]["Status"],
            "Reading",
        )

    def test_update_reading_state_failure(self):
        mock_resp = Mock()
        mock_resp.status_code = 500
        self.client._session = Mock()
        self.client._session.put.return_value = mock_resp

        result = self.client.update_reading_state("test-uuid", 0.5, STATUS_READING)
        self.assertFalse(result)

    # -- UUID resolution --

    def test_resolve_book_uuid_delegates_to_cwa_client(self):
        self.mock_cwa_client.get_book_uuid.return_value = "abcd-1234-uuid"
        result = self.client.resolve_book_uuid("42")
        self.assertEqual(result, "abcd-1234-uuid")
        self.mock_cwa_client.get_book_uuid.assert_called_once_with("42")

    def test_resolve_book_uuid_no_cwa_client(self):
        client = self._make_client(cwa_client=None)
        result = client.resolve_book_uuid("42")
        self.assertIsNone(result)


if __name__ == '__main__':
    unittest.main(verbosity=2)
