"""A KoSync server with no position for a mapped book must still receive one.

The bridge reads its own KoSync position through GET /syncs/progress. For a book
that has never had a KoSync position, that GET answers 404. Treating the 404 like
an outage kept KoSync out of every sync cycle, so the position was never written
and the next read 404'd again.
"""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.api.api_clients import KoSyncClient
from src.db.models import Book, State
from src.sync_clients.kosync_sync_client import KoSyncSyncClient
from src.sync_clients.sync_client_interface import LocatorResult

KOSYNC_DOC = "c" * 32
GENERATED_XPATH = "/body/DocFragment[3]/body/p[2]/text().0"


class TestKoSyncClientReportsHttpStatus(unittest.TestCase):
    def _client_returning(self, status_code):
        client = KoSyncClient()
        response = Mock(status_code=status_code)
        return client, patch.object(client.session, "get", return_value=response)

    def test_not_found_is_reported_in_metadata(self):
        client, get = self._client_returning(404)
        with get:
            self.assertEqual(client.get_progress_with_metadata("doc"), (None, None, {"_http_status": 404}))

    def test_server_error_is_reported_in_metadata(self):
        client, get = self._client_returning(500)
        with get:
            self.assertEqual(client.get_progress_with_metadata("doc"), (None, None, {"_http_status": 500}))


class TestKoSyncServiceStateForMissingPosition(unittest.TestCase):
    def _state(self, metadata, prev_state=None):
        api = Mock()
        api.get_progress_with_metadata.return_value = (None, None, metadata)
        api.is_configured.return_value = True
        client = KoSyncSyncClient(api, Mock())
        book = SimpleNamespace(kosync_doc_id=KOSYNC_DOC)
        return client.get_service_state(book, prev_state=prev_state, title_snip="Book")

    def test_not_found_without_a_saved_state_joins_at_zero(self):
        state = self._state({"_http_status": 404})

        self.assertIsNotNone(state)
        self.assertEqual(state.current["pct"], 0.0)
        self.assertIsNone(state.current["xpath"])
        self.assertEqual(state.delta, 0.0)

    def test_not_found_with_a_saved_state_is_not_read_as_a_rewind_to_zero(self):
        saved = State(abs_id="book", client_name="kosync", percentage=0.4, xpath="/body/DocFragment[1]/body/p[1].0")
        self.assertIsNone(self._state({"_http_status": 404}, prev_state=saved))

    def test_errors_still_leave_kosync_out(self):
        self.assertIsNone(self._state({"_http_status": 500}))
        self.assertIsNone(self._state({}))


class TestSyncCyclePushesToKoSyncWithoutAPosition(unittest.TestCase):
    """ABS moved before KoSync had any position; KoSync must still be brought up to it."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.temp_dir, ignore_errors=True)
        env = patch.dict(os.environ, {
            "DATA_DIR": self.temp_dir,
            "BOOKS_DIR": str(Path(self.temp_dir) / "books"),
            "ABS_SERVER": "http://localhost:13378",
            "ABS_TOKEN": "test-token",
        })
        env.start()
        self.addCleanup(env.stop)
        (Path(self.temp_dir) / "books").mkdir()
        (Path(self.temp_dir) / "books" / "night.epub").write_text("dummy epub content")
        transcript = Path(self.temp_dir) / "transcript.json"
        transcript.write_text(json.dumps([{"start": 0.0, "end": 1000.0, "text": "text"}]))

        self.book = Book(
            abs_id="night",
            abs_title="By Night in Chile",
            ebook_filename="night.epub",
            kosync_doc_id=KOSYNC_DOC,
            transcript_file=str(transcript),
            status="active",
            duration=1000.0,
        )
        # ABS's move to 11% was consumed by an earlier cycle that KoSync missed.
        self.states = [
            State(abs_id="night", client_name="abs", percentage=0.11, timestamp=110.0, last_updated=1791757881.5),
        ]

    def _assert_abs_position_is_pushed(self, use_bulk_shortcut=False):
        from src.sync_clients.abs_sync_client import ABSSyncClient
        from src.sync_manager import SyncManager

        abs_client = Mock()
        abs_client.is_configured.return_value = True
        abs_client.get_progress.return_value = {"currentTime": 110.0, "duration": 1000.0}
        abs_client.get_in_progress.return_value = [{"id": "night", "progress": 0.11, "duration": 1000.0}]
        # No bulk read, so the ABS position comes from get_progress.
        abs_client.get_all_progress_raw.return_value = None

        kosync_api = Mock()
        kosync_api.is_configured.return_value = True
        kosync_api.get_progress_with_metadata.return_value = (None, None, {"_http_status": 404})
        kosync_api.update_progress.return_value = True

        ebook_parser = Mock()
        ebook_parser.find_text_location.return_value = LocatorResult(percentage=0.11, xpath=GENERATED_XPATH, match_index=11)
        ebook_parser.get_sentence_level_ko_xpath.return_value = GENERATED_XPATH

        transcriber = Mock()
        transcriber.get_text_at_time.return_value = "text at eleven percent"
        transcriber.find_time_for_text.side_effect = (
            lambda _path, _text, hint_percentage=None, **_kw: (hint_percentage or 0.11) * 1000
        )

        database_service = Mock()
        database_service.get_all_books.return_value = [self.book]
        database_service.get_books_by_status.return_value = [self.book]
        database_service.get_book.return_value = self.book
        database_service.get_states_for_book.return_value = self.states
        database_service.get_all_states.return_value = self.states

        kosync = KoSyncSyncClient(kosync_api, ebook_parser)
        if use_bulk_shortcut:
            kosync.fetch_bulk_state = Mock(return_value={KOSYNC_DOC: self.book.abs_id})
        manager = SyncManager(
            abs_client=abs_client,
            transcriber=transcriber,
            ebook_parser=ebook_parser,
            database_service=database_service,
            sync_clients={
                "ABS": ABSSyncClient(abs_client, transcriber, ebook_parser),
                "KoSync": kosync,
            },
            epub_cache_dir=Path(self.temp_dir) / "epub_cache",
            data_dir=Path(self.temp_dir),
            books_dir=Path(self.temp_dir) / "books",
        )
        manager._automatch_hardcover = Mock()
        manager._sync_to_hardcover = Mock()

        manager.sync_cycle()

        kosync_api.update_progress.assert_called_once()
        doc_id, pct, xpath = kosync_api.update_progress.call_args.args
        self.assertEqual(doc_id, KOSYNC_DOC)
        self.assertAlmostEqual(pct, 0.11, places=2)
        self.assertEqual(xpath, "/body/DocFragment[3]/body/p[2].0")
        if use_bulk_shortcut:
            kosync_api.get_progress_with_metadata.assert_not_called()

    def test_abs_position_is_pushed_to_kosync_that_has_no_position(self):
        self._assert_abs_position_is_pushed()

    def test_bulk_shortcut_still_initializes_the_missing_kosync_position(self):
        self._assert_abs_position_is_pushed(use_bulk_shortcut=True)


if __name__ == "__main__":
    unittest.main()
