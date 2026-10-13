"""A KoSync server with no position for a mapped book must still receive one.

The bridge reads its own KoSync position through GET /syncs/progress. For a book
that has never had a KoSync position, that GET answers 404 (the original server
answers 200 with {}). Treating that like an outage kept KoSync out of every sync
cycle, so the position was never written.

KoSync joins as a follower_only client: it receives the other clients' agreed
position but never counts as a client appearing, so it cannot trigger a leader
decision between the clients that do have positions.
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
from src.sync_clients.sync_client_interface import (
    LocatorResult, ServiceState, SyncClient, SyncResult,
)

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


class PeerClient(SyncClient):
    """A third ebook client holding a fixed position, recording what it is sent."""

    def __init__(self, pct):
        super().__init__(Mock())
        self.pct = pct
        self.writes = []

    def is_configured(self):
        return True

    def get_service_state(self, book, prev_state, title_snip="", bulk_context=None):
        prev = prev_state.percentage if prev_state else 0
        return ServiceState(
            current={"pct": self.pct}, previous_pct=prev, delta=abs(self.pct - prev),
            threshold=0.01, is_configured=True, display=("Peer", "{prev:.1%} -> {curr:.1%}"),
            value_formatter=lambda v: f"{v * 100:.1f}%",
        )

    def get_text_from_current_state(self, book, state):
        return "peer text"

    def get_locator_from_text(self, txt, epub_file_name, hint_percentage):
        return LocatorResult(percentage=hint_percentage, xpath=GENERATED_XPATH, match_index=11)

    def update_progress(self, book, request):
        self.writes.append(request)
        pct = request.locator_result.percentage
        return SyncResult(pct, True, {"pct": pct})


class TestKoSyncServiceStateForMissingPosition(unittest.TestCase):
    def setUp(self):
        from src.utils.logging_utils import get_persistent_condition_logger
        get_persistent_condition_logger().reset()

    def _state(self, status, prev_state=None):
        api = Mock()
        api.get_progress_with_metadata.return_value = (None, None, {"_http_status": status} if status else {})
        api.is_configured.return_value = True
        client = KoSyncSyncClient(api, Mock())
        book = SimpleNamespace(abs_id="book", kosync_doc_id=KOSYNC_DOC)
        return client.get_service_state(book, prev_state=prev_state, title_snip="Book")

    def test_not_found_without_a_saved_state_is_a_follower_only(self):
        state = self._state(404)

        self.assertTrue(state.follower_only)
        self.assertEqual(state.delta, 0.0)
        self.assertNotIn("follower_only", state.current)

    def test_original_server_empty_200_is_a_follower_only(self):
        self.assertTrue(self._state(200).follower_only)

    def test_decision_is_logged_at_info(self):
        with self.assertLogs("src.sync_clients.kosync_sync_client", level="INFO") as logs:
            self._state(404)
        self.assertTrue(any("no stored position" in line for line in logs.output))

    def test_not_found_with_a_saved_state_is_not_read_as_a_rewind_to_zero(self):
        saved = State(abs_id="book", client_name="kosync", percentage=0.4, xpath="/body/DocFragment[1]/body/p[1].0")
        self.assertIsNone(self._state(404, prev_state=saved))

    def test_errors_and_outages_do_not_make_a_follower(self):
        self.assertIsNone(self._state(500))
        self.assertIsNone(self._state(None))

    def test_a_stored_position_is_not_missing(self):
        api = Mock()
        api.get_progress_with_metadata.return_value = (0.3, None, {"percentage": 0.3})
        api.is_configured.return_value = True
        state = KoSyncSyncClient(api, Mock()).get_service_state(
            SimpleNamespace(abs_id="book", kosync_doc_id=KOSYNC_DOC), prev_state=None)
        self.assertFalse(state.follower_only)


class TestOriginalServerEmptyResponse(unittest.TestCase):
    def test_empty_200_is_reported_with_its_status(self):
        client = KoSyncClient()
        response = Mock(status_code=200)
        response.json.return_value = {}
        with patch.object(client.session, "get", return_value=response):
            self.assertEqual(client.get_progress_with_metadata("doc"), (None, None, {"_http_status": 200}))

    def test_refused_fill_is_not_written_and_not_an_error(self):
        """The built-in server answers 409 when it keeps a position it already holds:
        the fill is not written (so no State is saved), and that is expected, not an
        error worth an ERROR line every attempt."""
        client = KoSyncClient()
        with patch.object(client, "is_configured", return_value=True),                 patch.object(client, "_is_local_server", return_value=True),                 patch.object(client.session, "put", return_value=Mock(status_code=409, text="")),                 self.assertNoLogs("src.api.api_clients", level="ERROR"):
            self.assertFalse(client.update_progress("doc", 0.11, "/body/p[1].0", fill_if_absent=True))


class TestSyncCycleFillsKoSyncWithoutAPosition(unittest.TestCase):
    """KoSync has no position for a mapped book; the cycle must fill it, and nothing else."""

    def setUp(self):
        from src.utils.logging_utils import get_persistent_condition_logger
        get_persistent_condition_logger().reset()
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
            abs_id="night", abs_title="By Night in Chile", ebook_filename="night.epub",
            kosync_doc_id=KOSYNC_DOC, transcript_file=str(transcript), status="active", duration=1000.0,
        )
        # ABS's move to 11% was consumed by an earlier cycle that KoSync missed.
        self.states = [
            State(abs_id="night", client_name="abs", percentage=0.11, timestamp=110.0, last_updated=1791757881.5),
        ]
        self.kosync_response = (None, None, {"_http_status": 404})
        self.kosync_update = True
        self.peer = None

    def _build(self, use_bulk_shortcut=False):
        from src.sync_clients.abs_sync_client import ABSSyncClient
        from src.sync_manager import SyncManager

        self.abs_client = abs_client = Mock()
        abs_client.is_configured.return_value = True
        abs_client.get_progress.return_value = {"currentTime": 110.0, "duration": 1000.0}
        abs_client.get_in_progress.return_value = [{"id": "night", "progress": 0.11, "duration": 1000.0}]
        abs_client.get_all_progress_raw.return_value = None

        self.kosync_api = kosync_api = Mock()
        kosync_api.is_configured.return_value = True
        kosync_api.get_progress_with_metadata.return_value = self.kosync_response
        kosync_api.update_progress.return_value = self.kosync_update

        self.ebook_parser = ebook_parser = Mock()
        ebook_parser.find_text_location.return_value = LocatorResult(percentage=0.11, xpath=GENERATED_XPATH, match_index=11)
        ebook_parser.get_sentence_level_ko_xpath.return_value = GENERATED_XPATH

        transcriber = Mock()
        transcriber.get_text_at_time.return_value = "text at eleven percent"
        transcriber.find_time_for_text.side_effect = (
            lambda _path, _text, hint_percentage=None, **_kw: (hint_percentage or 0.11) * 1000
        )

        self.database_service = database_service = Mock()
        database_service.get_all_books.return_value = [self.book]
        database_service.get_books_by_status.return_value = [self.book]
        database_service.get_book.return_value = self.book
        database_service.get_states_for_book.side_effect = lambda *_a, **_k: list(self.states)
        database_service.get_all_states.side_effect = lambda *_a, **_k: list(self.states)

        kosync = KoSyncSyncClient(kosync_api, ebook_parser)
        if use_bulk_shortcut:
            kosync.fetch_bulk_state = Mock(return_value={"documents_without_progress": {KOSYNC_DOC: self.book.abs_id}})
        clients = {"ABS": ABSSyncClient(abs_client, transcriber, ebook_parser), "KoSync": kosync}
        if self.peer is not None:
            clients["Peer"] = self.peer
        manager = SyncManager(
            abs_client=abs_client, transcriber=transcriber, ebook_parser=ebook_parser,
            database_service=database_service, sync_clients=clients,
            epub_cache_dir=Path(self.temp_dir) / "epub_cache", data_dir=Path(self.temp_dir),
            books_dir=Path(self.temp_dir) / "books",
        )
        manager._automatch_hardcover = Mock()
        manager._sync_to_hardcover = Mock()
        return manager

    def _saved_kosync_states(self):
        return [c.args[0] for c in self.database_service.save_state.call_args_list
                if c.args[0].client_name == "kosync"]

    def _assert_abs_position_is_pushed(self, use_bulk_shortcut=False):
        self._build(use_bulk_shortcut).sync_cycle()
        self.kosync_api.update_progress.assert_called_once()
        args, kwargs = self.kosync_api.update_progress.call_args
        doc_id, pct, xpath = args
        self.assertEqual(doc_id, KOSYNC_DOC)
        self.assertAlmostEqual(pct, 0.11, places=2)
        self.assertEqual(xpath, "/body/DocFragment[3]/body/p[2].0")
        self.assertEqual(kwargs, {"fill_if_absent": True})
        self.abs_client.update_progress.assert_not_called()
        if use_bulk_shortcut:
            self.kosync_api.get_progress_with_metadata.assert_not_called()

    def test_abs_position_is_pushed_to_kosync_that_has_no_position(self):
        self._assert_abs_position_is_pushed()

    def test_bulk_shortcut_still_initializes_the_missing_kosync_position(self):
        self._assert_abs_position_is_pushed(use_bulk_shortcut=True)

    def test_original_server_empty_200_joins_as_a_follower(self):
        self.kosync_response = (None, None, {"_http_status": 200})
        self._assert_abs_position_is_pushed()

    def test_stale_disagreement_among_other_clients_is_not_settled(self):
        """ABS (11%, already consumed) vs a stale 40% peer: KoSync appearing must not
        count as the read event that resolves it."""
        self.peer = PeerClient(0.40)
        self.states.append(State(abs_id="night", client_name="peer", percentage=0.40, last_updated=1791757881.5))
        self._build().sync_cycle()

        self.abs_client.update_progress.assert_not_called()
        self.assertEqual(self.peer.writes, [])
        self.kosync_api.update_progress.assert_not_called()

    def test_agreeing_clients_are_left_alone_and_only_kosync_is_written(self):
        self.peer = PeerClient(0.11)
        self.states.append(State(abs_id="night", client_name="peer", percentage=0.11, last_updated=1791757881.5))
        self._build().sync_cycle()

        self.kosync_api.update_progress.assert_called_once()
        self.assertEqual(self.peer.writes, [])
        self.abs_client.update_progress.assert_not_called()
        self.assertEqual([s.percentage for s in self._saved_kosync_states()], [0.11])

    def test_failed_write_is_not_retried_within_the_cooldown(self):
        self.kosync_update = False
        manager = self._build()
        manager.sync_cycle()
        manager.sync_cycle()

        self.assertEqual(self.kosync_api.update_progress.call_count, 1)
        self.assertEqual(self._saved_kosync_states(), [])

    def test_unresolvable_position_is_not_retried_within_the_cooldown(self):
        manager = self._build()
        self.ebook_parser.get_sentence_level_ko_xpath.return_value = None
        manager.sync_cycle()
        manager.sync_cycle()

        self.assertEqual(self.ebook_parser.get_sentence_level_ko_xpath.call_count, 1)
        self.kosync_api.update_progress.assert_not_called()

    def test_real_activity_still_includes_kosync_during_the_cooldown(self):
        from src import sync_manager

        self.kosync_update = False
        manager = self._build()
        manager.sync_cycle()
        self.assertEqual(self.kosync_api.update_progress.call_count, 1)

        # ABS moves on: KoSync is a follower of that change despite the cooldown.
        self.states[0] = State(abs_id="night", client_name="abs", percentage=0.05, timestamp=50.0, last_updated=1791757881.5)
        manager.sync_cycle()
        self.assertEqual(self.kosync_api.update_progress.call_count, 2)

        # Cooldown over: the quiet-book fill is attempted again.
        self.states[0] = State(abs_id="night", client_name="abs", percentage=0.11, timestamp=110.0, last_updated=1791757881.5)
        with patch.object(sync_manager, "_FOLLOWER_INIT_COOLDOWN_SECONDS", 0):
            manager.sync_cycle()
        self.assertEqual(self.kosync_api.update_progress.call_count, 3)


class TestFollowerNeverLeads(unittest.TestCase):
    def test_determine_leader_skips_follower_only_states(self):
        from src.sync_manager import SyncManager

        def state(pct, follower_only=False):
            return ServiceState(
                current={"pct": pct}, previous_pct=0, delta=0, threshold=0.01, is_configured=True,
                display=("X", "{prev}{curr}"), value_formatter=str, follower_only=follower_only,
            )

        manager = SyncManager.__new__(SyncManager)
        manager.sync_clients = {"Peer": PeerClient(0.2), "KoSync": PeerClient(0.9)}
        config = {"Peer": state(0.2), "KoSync": state(0.9, follower_only=True)}
        with patch.object(SyncManager, "_normalize_for_cross_format_comparison", return_value={}), \
                patch.object(SyncManager, "_has_significant_delta", return_value=True):
            leader, pct = manager._determine_leader(config, SimpleNamespace(), "book", "Book")
        self.assertEqual(leader, "Peer")


if __name__ == "__main__":
    unittest.main()
