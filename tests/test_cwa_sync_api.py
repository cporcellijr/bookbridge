#!/usr/bin/env python3
"""Unit tests for CWASyncApi."""

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.api.cwa_sync_api import CWASyncApi, STATUS_READING, STATUS_FINISHED, STATUS_READY
from src.utils.logging_utils import get_persistent_condition_logger


class TestCWASyncApi(unittest.TestCase):

    def _make_client(self, server='http://cwa.local:8083', token='abc123token',
                     enabled='true', cwa_client=None):
        """Create a client with server/token snapshotted from env.

        The enable flag is read per call, not snapshotted — the class is a DI
        Singleton, so a flag captured at construction would outlive an admin
        switching CWA off. The patch therefore has to stay up for the whole test,
        not just the constructor.
        """
        patcher = patch.dict('os.environ', {
            'CWA_SERVER': server,
            'CWA_SYNC_ENABLED': enabled,
            'CWA_SYNC_TOKEN': token,
        })
        patcher.start()
        self.addCleanup(patcher.stop)
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

    def test_update_without_span_preserves_device_bookmark(self):
        """With no span to offer, the device's own locator must survive untouched.

        7.4.1 sent ``{"Source": "", "Type": "", "Value": ""}`` here on the theory that a
        stale span would rewind the device. It does not help: the Kobo keeps its own
        local bookmark either way, so blanking only destroyed the server-side copy —
        live, it left 0 of 370 CWA bookmarks with a span while users still reported the
        rewind (#364). The cure is writing a correct span (see the next test), not
        erasing the old one.
        """
        stored_bookmark = {
            "ProgressPercent": 12.0,
            "location_source": "OEBPS/chapter.xhtml",
            "location_type": "KoboSpan",
            "location_value": "#point(/1/4/2/2:0)",
        }

        def apply_cwa_update(_url, json, timeout):
            bookmark = json["ReadingStates"][0]["CurrentBookmark"]
            stored_bookmark["ProgressPercent"] = bookmark["ProgressPercent"]
            location = bookmark["Location"]
            # This matches CWA v4.0.6: null preserves all three location fields,
            # while a truthy object replaces them.
            if location:
                stored_bookmark["location_source"] = location["Source"]
                stored_bookmark["location_type"] = location["Type"]
                stored_bookmark["location_value"] = location["Value"]
            response = Mock()
            response.status_code = 200
            response.json.return_value = {"RequestResult": "Success"}
            return response

        self.client._session = Mock()
        self.client._session.put.side_effect = apply_cwa_update

        self.assertTrue(
            self.client.update_reading_state("test-uuid", 0.16, STATUS_READING)
        )
        self.assertEqual(stored_bookmark["ProgressPercent"], 16.0)
        self.assertEqual(
            stored_bookmark["location_value"],
            "#point(/1/4/2/2:0)",
            "Writing no span must not wipe the locator the device wrote",
        )
        self.assertEqual(stored_bookmark["location_source"], "OEBPS/chapter.xhtml")

        # CWA reads request_bookmark["Location"] unguarded and 400s without the key.
        sent = self.client._session.put.call_args.kwargs["json"]
        self.assertIn("Location", sent["ReadingStates"][0]["CurrentBookmark"])
        self.assertIsNone(sent["ReadingStates"][0]["CurrentBookmark"]["Location"])

    def test_update_with_span_writes_the_kobo_bookmark(self):
        """A resolved span replaces the stored locator so the device actually moves."""
        stored_bookmark = {
            "ProgressPercent": 12.0,
            "location_source": "OEBPS/Text/part0001.xhtml",
            "location_type": "KoboSpan",
            "location_value": "kobo.4.1",
        }

        def apply_cwa_update(_url, json, timeout):
            bookmark = json["ReadingStates"][0]["CurrentBookmark"]
            stored_bookmark["ProgressPercent"] = bookmark["ProgressPercent"]
            location = bookmark["Location"]
            if location:
                stored_bookmark["location_source"] = location["Source"]
                stored_bookmark["location_type"] = location["Type"]
                stored_bookmark["location_value"] = location["Value"]
            response = Mock()
            response.status_code = 200
            response.json.return_value = {"RequestResult": "Success"}
            return response

        self.client._session = Mock()
        self.client._session.put.side_effect = apply_cwa_update

        self.assertTrue(
            self.client.update_reading_state(
                "test-uuid", 0.16, STATUS_READING,
                location={
                    "Source": "OEBPS/Text/part0024.xhtml",
                    "Type": "KoboSpan",
                    "Value": "kobo.114.4",
                },
            )
        )
        self.assertEqual(stored_bookmark["ProgressPercent"], 16.0)
        self.assertEqual(stored_bookmark["location_value"], "kobo.114.4")
        self.assertEqual(stored_bookmark["location_source"], "OEBPS/Text/part0024.xhtml")
        self.assertEqual(stored_bookmark["location_type"], "KoboSpan")

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
        self.mock_cwa_client.get_book_uuid.assert_called_once_with("42", search_hints=None)

    def test_resolve_book_uuid_forwards_search_hints(self):
        # #427: the numeric Calibre id can't be searched for itself, so the
        # ordered hint chain must reach CWAClient.get_book_uuid unchanged.
        self.mock_cwa_client.get_book_uuid.return_value = "abcd-1234-uuid"
        result = self.client.resolve_book_uuid("1519", search_hints=["Dungeon Crawler Carl"])
        self.assertEqual(result, "abcd-1234-uuid")
        self.mock_cwa_client.get_book_uuid.assert_called_once_with(
            "1519", search_hints=["Dungeon Crawler Carl"]
        )

    def test_resolve_book_uuid_no_cwa_client(self):
        client = self._make_client(cwa_client=None)
        result = client.resolve_book_uuid("42")
        self.assertIsNone(result)

    # -- Connection-check repeat suppression --

    def test_connection_error_repeats_are_suppressed_at_error_level(self):
        """An unreachable CWA host logs once at ERROR, then drops to DEBUG.

        The check runs every cycle and the condition holds until the host comes
        back, so it re-logged the identical ERROR forever.
        """
        get_persistent_condition_logger().reset()
        self.addCleanup(get_persistent_condition_logger().reset)
        self.client._session = Mock()
        self.client._session.get.side_effect = OSError("connection refused")

        with self.assertLogs('src.api.cwa_sync_api', level='DEBUG') as captured:
            for _ in range(5):
                self.assertFalse(self.client.check_connection())

        errors = [r for r in captured.records
                  if r.levelname == 'ERROR' and 'CWA Sync connection error' in r.getMessage()]
        self.assertEqual(len(errors), 1)

    def test_connection_recovery_is_announced_once(self):
        """Recovery is announced at INFO after the condition cleared."""
        get_persistent_condition_logger().reset()
        self.addCleanup(get_persistent_condition_logger().reset)
        self.client._session = Mock()
        self.client._session.get.side_effect = OSError("connection refused")
        self.client.check_connection()

        ok = Mock()
        ok.status_code = 200
        self.client._session.get.side_effect = None
        self.client._session.get.return_value = ok

        with self.assertLogs('src.api.cwa_sync_api', level='INFO') as captured:
            self.assertTrue(self.client.check_connection())
            self.assertTrue(self.client.check_connection())

        recovered = [r for r in captured.records if 'connection recovered' in r.getMessage()]
        self.assertEqual(len(recovered), 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
