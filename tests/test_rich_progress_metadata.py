"""
Rich progress metadata (Phase 1: capture + persist, zero behavior change).

Covers the shared helpers, each client's capture of its service's own
"position last changed" timestamp (using live-verified payload shapes), and
the State persistence round-trip.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault('DATA_DIR', 'test_data')
os.environ.setdefault('BOOKS_DIR', 'test_data')

from src.utils.progress_metadata import (
    parse_service_timestamp,
    derive_locator_source,
    extract_locator_json,
    state_metadata_kwargs,
)

from datetime import datetime, timezone


def _utc_epoch(*args, microsecond=0):
    return datetime(*args, microsecond=microsecond, tzinfo=timezone.utc).timestamp()


GRIMMORY_TS = _utc_epoch(2026, 6, 2, 22, 1, 1)             # "2026-06-02T22:01:01Z"
BOOKORBIT_TS = _utc_epoch(2026, 7, 2, 14, 21, 55, microsecond=822000)  # "...T14:21:55.822Z"


class TestParseServiceTimestamp(unittest.TestCase):
    def test_iso_zulu(self):
        # Grimmory lastReadTime shape (verified live)
        self.assertEqual(parse_service_timestamp("2026-06-02T22:01:01Z"), GRIMMORY_TS)

    def test_iso_fractional(self):
        # BookOrbit updatedAt shape (verified live)
        self.assertAlmostEqual(parse_service_timestamp("2026-07-02T14:21:55.822Z"), BOOKORBIT_TS, places=3)

    def test_epoch_seconds(self):
        self.assertEqual(parse_service_timestamp(1751400000), 1751400000.0)

    def test_epoch_milliseconds(self):
        # Storyteller position timestamp shape (verified live)
        self.assertAlmostEqual(parse_service_timestamp(1782755169059), 1782755169.059, places=3)

    def test_numeric_string(self):
        self.assertEqual(parse_service_timestamp("1751400000"), 1751400000.0)

    def test_koreader_device_datetime(self):
        self.assertEqual(parse_service_timestamp("2026-06-02 22:01:01"), GRIMMORY_TS)

    def test_useless_values_return_none(self):
        for value in (None, 0, 0.0, "", "   ", "not-a-date", -5):
            self.assertIsNone(parse_service_timestamp(value), msg=repr(value))


class TestLocatorHelpers(unittest.TestCase):
    def test_locator_source_ladder(self):
        cases = [
            ({"position": 48, "href": "text/p9.html", "cfi": "epubcfi(/6/4!)"}, "position+href"),
            ({"cfi": "epubcfi(/6/4!)", "href": "OEBPS/ch06.xhtml"}, "cfi+href"),
            ({"cfi": "epubcfi(/6/4!)"}, "cfi"),
            ({"href": "OEBPS/ch06.xhtml"}, "href"),
            ({"xpath": "/body/DocFragment[7]/p[3]"}, "xpath"),
            ({"pct": 0.5}, "percentage"),
            ({}, None),
        ]
        for current, expected in cases:
            self.assertEqual(derive_locator_source(current), expected, msg=str(current))

    def test_locator_json_excludes_core_and_private_keys(self):
        current = {
            "pct": 0.5, "ts": 1234.0, "service_updated_at": 1751400000.0, "status": "Reading",
            "_kosync_recent_external_put": True,
            "cfi": "epubcfi(/6/4!)", "href": "x.xhtml", "page": 12, "none_field": None,
        }
        payload = json.loads(extract_locator_json(current))
        self.assertEqual(payload, {"cfi": "epubcfi(/6/4!)", "href": "x.xhtml", "page": 12})

    def test_locator_json_none_when_nothing_locatorish(self):
        self.assertIsNone(extract_locator_json({"pct": 0.4, "ts": 12.0}))

    def test_state_metadata_kwargs(self):
        kwargs = state_metadata_kwargs({
            "pct": 0.3, "cfi": "epubcfi(/6/4!)", "href": "c.xhtml",
            "service_updated_at": 1751400000.0, "status": "READING",
        })
        self.assertEqual(kwargs["service_updated_at"], 1751400000.0)
        self.assertEqual(kwargs["status"], "READING")
        self.assertEqual(kwargs["locator_source"], "cfi+href")
        self.assertIn("cfi", json.loads(kwargs["locator_json"]))


class TestCwaCapture(unittest.TestCase):
    """CWA freshness must come from CurrentBookmark.LastModified, never the
    outer LastModified (which moves on status changes and our own writes —
    verified live 2026-07-02)."""

    _LIVE_SHAPED = [{
        "Created": "2026-05-15T20:43:26Z",
        "LastModified": "2026-06-27T14:00:43Z",
        "PriorityTimestamp": "2026-06-27T14:00:43Z",
        "CurrentBookmark": {
            "ProgressPercent": 36.24,
            "ContentSourceProgressPercent": 36.24,
            "LastModified": "2026-06-25T16:11:40Z",
        },
        "StatusInfo": {"Status": "Reading", "LastModified": "2026-06-23T22:48:30Z"},
    }]

    def _api(self):
        from src.api.cwa_sync_api import CWASyncApi
        api = CWASyncApi.__new__(CWASyncApi)
        api._server = "http://cwa"
        api._token = "tok"
        api._enabled = True
        api._timeout = 5
        api._session = MagicMock()
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = self._LIVE_SHAPED
        api._session.get.return_value = response
        return api

    def test_get_reading_state_exposes_bookmark_timestamp(self):
        state = self._api().get_reading_state("uuid-1")
        self.assertEqual(state["bookmark_last_modified"], "2026-06-25T16:11:40Z")
        self.assertEqual(state["status"], "Reading")

    def test_sync_client_uses_bookmark_not_outer_timestamp(self):
        from src.sync_clients.cwa_sync_client import CWASyncClient
        client = CWASyncClient(self._api(), MagicMock(), MagicMock())
        book = MagicMock(ebook_source="CWA", ebook_source_id="42",
                         original_ebook_filename="x.epub", ebook_filename="x.epub",
                         abs_title="X")
        client._resolve_uuid = lambda b: "uuid-1"
        state = client.get_service_state(book, prev_state=None)
        expected = parse_service_timestamp("2026-06-25T16:11:40Z")
        self.assertEqual(state.current["service_updated_at"], expected)
        self.assertNotEqual(state.current["service_updated_at"],
                            parse_service_timestamp("2026-06-27T14:00:43Z"))
        self.assertEqual(state.current["status"], "Reading")


class TestGrimmoryCapture(unittest.TestCase):
    _LIVE_SHAPED = {
        "id": 10194,
        "readStatus": "READING",
        "lastReadTime": "2026-06-02T22:01:01Z",
        "primaryFile": {"bookType": "EPUB"},
        "epubProgress": {
            "cfi": "epubcfi(/6/32!/4/14/8:0)",
            "href": "OEBPS/14_Chapter_06.xhtml",
            "percentage": 27.5,
            "contentSourceProgressPercent": 27.5,
            "ttsPositionCfi": None,
        },
    }

    def test_get_progress_rich_parses_live_shape(self):
        from src.api.booklore_client import BookloreClient
        client = BookloreClient.__new__(BookloreClient)
        client.find_book_by_filename = MagicMock(return_value={"id": 10194})
        response = MagicMock(status_code=200)
        client._make_request = MagicMock(return_value=response)
        client._parse_json_response = MagicMock(return_value=self._LIVE_SHAPED)
        rich = client.get_progress_rich("x.epub")
        self.assertEqual(rich["pct"], 0.275)
        self.assertEqual(rich["href"], "OEBPS/14_Chapter_06.xhtml")
        self.assertEqual(rich["status"], "READING")
        self.assertEqual(rich["last_read_time"], "2026-06-02T22:01:01Z")

    def test_sync_client_captures_rich_fields(self):
        from src.sync_clients.booklore_sync_client import BookloreSyncClient
        bl = MagicMock()
        bl.get_progress_rich.return_value = {
            "pct": 0.275, "cfi": "epubcfi(/6/32!/4/14/8:0)",
            "href": "OEBPS/14_Chapter_06.xhtml",
            "last_read_time": "2026-06-02T22:01:01Z", "status": "READING",
        }
        client = BookloreSyncClient(bl, MagicMock())
        book = MagicMock(original_ebook_filename="x.epub", ebook_filename="x.epub")
        state = client.get_service_state(book, prev_state=None)
        self.assertEqual(state.current["service_updated_at"], GRIMMORY_TS)
        self.assertEqual(state.current["status"], "READING")
        self.assertEqual(state.current["href"], "OEBPS/14_Chapter_06.xhtml")

    def test_sync_client_falls_back_when_rich_is_not_a_dict(self):
        from src.sync_clients.booklore_sync_client import BookloreSyncClient
        bl = MagicMock()
        bl.get_progress_rich.return_value = MagicMock()  # mocked/legacy client
        bl.get_progress.return_value = (0.5, "epubcfi(/6/4!)")
        client = BookloreSyncClient(bl, MagicMock())
        book = MagicMock(original_ebook_filename="x.epub", ebook_filename="x.epub")
        state = client.get_service_state(book, prev_state=None)
        self.assertEqual(state.current["pct"], 0.5)
        self.assertNotIn("service_updated_at", state.current)

    def test_sync_client_treats_missing_percentage_as_expected_empty_state(self):
        from src.sync_clients.booklore_sync_client import BookloreSyncClient
        bl = MagicMock()
        bl.get_progress_rich.return_value = {"pct": None, "cfi": None}
        client = BookloreSyncClient(bl, MagicMock())
        book = MagicMock(original_ebook_filename="x.epub", ebook_filename="x.epub")

        with self.assertNoLogs(
            "src.sync_clients.booklore_sync_client", level="WARNING"
        ):
            state = client.get_service_state(book, prev_state=None)

        self.assertIsNone(state)


class TestBookOrbitCapture(unittest.TestCase):
    def _client(self, payload):
        from src.api.bookorbit_client import BookOrbitClient
        client = BookOrbitClient()
        response = MagicMock(status_code=200)
        response.json.return_value = payload
        client._make_request = MagicMock(return_value=response)
        return client

    def test_ebook_progress_rich_parses_live_shape(self):
        client = self._client([{
            "fileId": 1947, "cfi": "epubcfi(/6/8!/4/2:0)", "pageNumber": 12,
            "percentage": 41.5, "updatedAt": "2026-07-02T14:21:55.822Z",
            "koreaderProgress": "/body/DocFragment[8]/body/p[10]/text().5",
        }])
        rich = client.get_ebook_progress_rich(1583)
        self.assertAlmostEqual(rich["pct"], 0.415)
        self.assertEqual(rich["file_id"], 1947)
        self.assertEqual(rich["page_number"], 12)
        self.assertEqual(rich["updated_at"], "2026-07-02T14:21:55.822Z")
        self.assertEqual(rich["koreader_progress"], "/body/DocFragment[8]/body/p[10]/text().5")
        # legacy tuple wrapper still works
        pct, cfi = client.get_ebook_progress(1583)
        self.assertAlmostEqual(pct, 0.415)
        self.assertEqual(cfi, "epubcfi(/6/8!/4/2:0)")

    def test_ebook_progress_rich_unstarted_baseline(self):
        client = self._client(None)
        client._make_request.return_value.json.return_value = None
        rich = client.get_ebook_progress_rich(1)
        self.assertEqual(rich["pct"], 0.0)
        self.assertIsNone(rich["updated_at"])

    def test_audio_progress_includes_updated_at(self):
        client = self._client({
            "userId": 1, "bookId": 4345, "percentage": 20.0,
            "currentFileId": 9379, "positionSeconds": 194.0,
            "updatedAt": "2026-07-02T13:45:48.701Z",
        })
        progress = client.get_audiobook_progress(4345)
        self.assertEqual(progress["updated_at"], "2026-07-02T13:45:48.701Z")

    def test_ebook_sync_client_captures_metadata(self):
        from src.sync_clients.bookorbit_sync_client import BookOrbitSyncClient
        bo = MagicMock()
        bo.get_ebook_progress_rich.return_value = {
            "pct": 0.415, "cfi": "epubcfi(/6/8!)", "updated_at": "2026-07-02T14:21:55.822Z",
            "file_id": 1947, "page_number": 12, "koreader_progress": "/body/DocFragment[8]",
        }
        client = BookOrbitSyncClient(bo, MagicMock())
        book = MagicMock(ebook_source="BookOrbit", ebook_source_id="1583",
                         original_ebook_filename="x.epub", ebook_filename="x.epub")
        state = client.get_service_state(book, prev_state=None)
        self.assertAlmostEqual(state.current["service_updated_at"], BOOKORBIT_TS, places=3)
        self.assertEqual(state.current["page"], 12)
        self.assertEqual(state.current["file_id"], 1947)
        self.assertEqual(state.current["koreader_progress"], "/body/DocFragment[8]")

    def test_audio_sync_client_captures_service_timestamp(self):
        from src.sync_clients.bookorbit_audio_sync_client import BookOrbitAudioSyncClient
        bo = MagicMock()
        bo.get_audiobook_progress.return_value = {
            "pct": 0.2, "position_seconds": 194.0, "current_file_id": 9379,
            "updated_at": "2026-07-02T13:45:48.701Z",
        }
        bo.get_audiobook_info.return_value = {"duration_seconds": 1000, "tracks": [], "primary_file_id": 9379}
        client = BookOrbitAudioSyncClient(bo, ebook_parser=None)
        book = MagicMock(audio_source="BookOrbit", audio_source_id="4345",
                         audio_provider_book_id=None, audio_duration=1000, duration=1000)
        state = client.get_service_state(book, prev_state=None)
        self.assertIsNotNone(state.current.get("service_updated_at"))


class TestKosyncAndStorytellerCapture(unittest.TestCase):
    def test_kosync_lifts_put_timestamp_from_metadata(self):
        from src.sync_clients.kosync_sync_client import KoSyncSyncClient
        ks = MagicMock()
        ks.get_progress_with_metadata.return_value = (
            0.42, "/body/DocFragment[7]/p[3]",
            {"percentage": 0.42, "progress": "/body/DocFragment[7]/p[3]", "timestamp": 1751400000},
        )
        client = KoSyncSyncClient(ks, MagicMock())
        book = MagicMock(kosync_doc_id="a" * 32)
        state = client.get_service_state(book, prev_state=None)
        self.assertEqual(state.current["service_updated_at"], 1751400000.0)

    def test_kosync_zero_timestamp_means_absent(self):
        from src.sync_clients.kosync_sync_client import KoSyncSyncClient
        ks = MagicMock()
        ks.get_progress_with_metadata.return_value = (0.42, "/body/x", {"timestamp": 0})
        client = KoSyncSyncClient(ks, MagicMock())
        book = MagicMock(kosync_doc_id="a" * 32)
        state = client.get_service_state(book, prev_state=None)
        self.assertNotIn("service_updated_at", state.current)


class TestStatePersistence(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        from src.db.database_service import DatabaseService
        self.db = DatabaseService(str(Path(self.temp_dir) / 'test.db'))
        from src.db.models import Book
        self.db.save_book(Book(abs_id="book-1", abs_title="T", kosync_doc_id="k" * 32))

    def tearDown(self):
        if hasattr(self.db, 'db_manager'):
            self.db.db_manager.close()
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_rich_fields_round_trip(self):
        from src.db.models import State
        current = {
            "pct": 0.42, "cfi": "epubcfi(/6/32!)", "href": "OEBPS/ch06.xhtml",
            "service_updated_at": 1780524061.0, "status": "READING",
        }
        self.db.save_state(State(
            abs_id="book-1", client_name="booklore", last_updated=1.0,
            percentage=0.42, cfi=current["cfi"],
            **state_metadata_kwargs(current),
        ))
        row = self.db.get_state("book-1", "booklore")
        self.assertEqual(row.service_updated_at, 1780524061.0)
        self.assertEqual(row.status, "READING")
        self.assertEqual(row.locator_source, "cfi+href")
        self.assertEqual(json.loads(row.locator_json)["href"], "OEBPS/ch06.xhtml")

    def test_update_path_persists_rich_fields_on_existing_rows(self):
        from src.db.models import State
        # legacy-style row without rich fields
        self.db.save_state(State(abs_id="book-1", client_name="kosync",
                                 last_updated=1.0, percentage=0.1))
        row = self.db.get_state("book-1", "kosync")
        self.assertIsNone(row.service_updated_at)
        # later write carries them — update path must copy them onto the row
        self.db.save_state(State(
            abs_id="book-1", client_name="kosync", last_updated=2.0, percentage=0.2,
            **state_metadata_kwargs({"pct": 0.2, "xpath": "/body/x", "service_updated_at": 1751400000}),
        ))
        row = self.db.get_state("book-1", "kosync")
        self.assertEqual(row.service_updated_at, 1751400000.0)
        self.assertEqual(row.locator_source, "xpath")


if __name__ == '__main__':
    unittest.main()
