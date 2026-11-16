#!/usr/bin/env python3
"""
Regression tests for issue #357.

A reader changed their Audiobookshelf library layout/paths. ABS re-ingested the
moved files as brand new library items, so the bridge's stored `abs_id` values
went stale. Every push then failed with ABS's 404 "Library item not found" and
the bridge only logged:

    ❌ Failed to create ABS session: 404 - {"error":"Library item not found"}
    ❌ Failed to create ABS session for item 'test-abs-id-stale'

The book stayed `active` and retried forever with nothing surfaced to the user.
The fix probes ABS to confirm the item is really gone, and marks the book
'error' at the sync-cycle choke point so the dashboard shows it needs a re-match.

Critically, the probe is tri-state: only a definitive 404 may mark a book
broken. A transient outage or an ambiguous status must leave the book alone.
"""

import logging
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.base_sync_test import BaseSyncCycleTestCase
from src.api.api_clients import ABSClient
from src.db.models import Book
from src.sync_clients.abs_ebook_sync_client import ABSEbookSyncClient
from src.sync_clients.abs_sync_client import ABSSyncClient
from src.sync_clients.sync_client_interface import (
    ABS_ITEM_NOT_FOUND,
    LocatorResult,
    UpdateProgressRequest,
)

ABS_NOT_FOUND_BODY = '{"error":"Library item not found"}'


class TestABSItemExistsProbe(unittest.TestCase):
    """ABSClient.item_exists must only report False on a real 404."""

    def setUp(self):
        self.client = ABSClient()
        self.client.is_configured = Mock(return_value=True)
        self.client._update_session_headers = Mock()
        self.client.session = Mock()

    def _respond(self, status_code: int, text: str = ""):
        response = Mock()
        response.status_code = status_code
        response.text = text
        self.client.session.get.return_value = response

    def test_present_item_returns_true(self):
        self._respond(200)
        self.assertIs(self.client.item_exists('abs-1'), True)

    def test_missing_item_returns_false(self):
        self._respond(404, ABS_NOT_FOUND_BODY)
        self.assertIs(self.client.item_exists('abs-1'), False)

    def test_server_error_returns_none(self):
        self._respond(500)
        self.assertIsNone(
            self.client.item_exists('abs-1'),
            "A 5xx must be 'unknown', never proof the item is gone",
        )

    def test_unauthorized_returns_none(self):
        self._respond(401)
        self.assertIsNone(self.client.item_exists('abs-1'))

    def test_transport_error_returns_none(self):
        self.client.session.get.side_effect = ConnectionError("boom")
        self.assertIsNone(self.client.item_exists('abs-1'))

    def test_unconfigured_returns_none(self):
        self.client.is_configured.return_value = False
        self.assertIsNone(self.client.item_exists('abs-1'))
        self.client.session.get.assert_not_called()


class TestABSSyncClientStaleItemCode(unittest.TestCase):
    """The audiobook client tags a failed write only when ABS confirms the 404."""

    def setUp(self):
        self.abs_client = Mock()
        self.client = ABSSyncClient(self.abs_client, Mock(), Mock())
        self.failed_write = {
            "success": False,
            "code": None,
            "reason": "Failed to create ABS session for item test-abs-id-stale",
        }

    def test_confirmed_404_yields_error_code(self):
        self.abs_client.item_exists.return_value = False
        self.assertEqual(
            self.client._stale_item_error_code('test-abs-id-stale', self.failed_write),
            ABS_ITEM_NOT_FOUND,
        )
        self.abs_client.item_exists.assert_called_once_with('test-abs-id-stale')

    def test_item_still_present_yields_no_error_code(self):
        self.abs_client.item_exists.return_value = True
        self.assertIsNone(
            self.client._stale_item_error_code('test-abs-id-stale', self.failed_write)
        )

    def test_indeterminate_probe_yields_no_error_code(self):
        self.abs_client.item_exists.return_value = None
        self.assertIsNone(
            self.client._stale_item_error_code('test-abs-id-stale', self.failed_write),
            "An outage must never mark a book broken",
        )

    def test_probe_exception_yields_no_error_code(self):
        self.abs_client.item_exists.side_effect = RuntimeError("probe exploded")
        self.assertIsNone(
            self.client._stale_item_error_code('test-abs-id-stale', self.failed_write)
        )

    def test_successful_write_is_never_probed(self):
        self.assertIsNone(
            self.client._stale_item_error_code('test-abs-id-stale', {"success": True})
        )
        self.abs_client.item_exists.assert_not_called()

    def test_non_dict_write_result_is_handled(self):
        self.abs_client.item_exists.return_value = False
        self.assertEqual(
            self.client._stale_item_error_code('test-abs-id-stale', False),
            ABS_ITEM_NOT_FOUND,
        )
        self.assertIsNone(
            self.client._stale_item_error_code('test-abs-id-stale', True)
        )


class TestABSEbookSyncClientStaleItemCode(unittest.TestCase):
    """The ebook client probes the item it actually wrote to."""

    def setUp(self):
        self.abs_client = Mock()
        self.client = ABSEbookSyncClient(self.abs_client, Mock())
        self.book = Book(
            abs_id='audio-item-id',
            abs_title='Stale Ebook Book',
            ebook_filename='test-book.epub',
        )
        self.book.abs_ebook_item_id = 'li_ebook_item'

    def test_failed_write_on_missing_item_yields_error_code(self):
        self.abs_client.update_ebook_progress.return_value = False
        self.abs_client.item_exists.return_value = False

        result = self.client.update_progress(
            self.book,
            UpdateProgressRequest(LocatorResult(percentage=0.42, cfi='epubcfi(/6/4!/4/2)')),
        )

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, ABS_ITEM_NOT_FOUND)
        # The ABS ebook item id, not the audiobook id, is what went stale.
        self.abs_client.item_exists.assert_called_once_with('li_ebook_item')

    def test_successful_write_is_never_probed(self):
        self.abs_client.update_ebook_progress.return_value = True

        result = self.client.update_progress(
            self.book,
            UpdateProgressRequest(LocatorResult(percentage=0.42, cfi='epubcfi(/6/4!/4/2)')),
        )

        self.assertTrue(result.success)
        self.assertIsNone(result.error_code)
        self.abs_client.item_exists.assert_not_called()

    def test_missing_cfi_does_not_probe(self):
        result = self.client.update_progress(
            self.book, UpdateProgressRequest(LocatorResult(percentage=0.42))
        )

        self.assertFalse(result.success)
        self.assertIsNone(result.error_code)
        self.abs_client.item_exists.assert_not_called()


class TestStaleABSItemMarksBookError(BaseSyncCycleTestCase):
    """Integration: the real sync cycle marks a stale mapping 'error'."""

    def get_test_mapping(self):
        return {
            'abs_id': 'test-abs-id-stale',
            'abs_title': 'Stale Mapping Test Book',
            'kosync_doc_id': 'test-kosync-doc-stale',
            'ebook_filename': 'test-book.epub',
            'transcript_file': str(Path(self.temp_dir) / 'test_transcript.json'),
            'status': 'active',
            'duration': 1000.0,
        }

    def get_test_state_data(self):
        return {
            'abs': {'pct': 0.10, 'ts': 100.0, 'last_updated': 1234567890},
            'kosync': {'pct': 0.10, 'last_updated': 1234567890},
        }

    def get_expected_leader(self):
        return "KoSync"

    def get_expected_final_percentage(self):
        return 0.60

    def get_progress_mock_returns(self):
        return {
            'abs_progress': {'currentTime': 100.0, 'duration': 1000},  # 10%
            'abs_in_progress': [{'id': 'test-abs-id-stale', 'progress': 0.10, 'duration': 1000}],
            'kosync_progress': (0.60, "/body/DocFragment[1]/body/p[1]"),
            'storyteller_progress': (0.0, 0.0, None, None),
            'booklore_progress': (0.0, None),
        }

    def _build_manager(self, item_exists):
        """Real SyncManager with mocked clients; the ABS write fails and the
        existence probe answers `item_exists`."""
        mocks = self.setup_common_mocks()

        # KoSync leads at 60% and its locator resolves cleanly, so the cycle
        # reaches the ABS write instead of tripping the collapse guard.
        mocks['ebook_parser'].resolve_xpath.return_value = "text near 60 percent"
        mocks['ebook_parser'].get_text_at_percentage.return_value = "text near 60 percent"
        mocks['ebook_parser'].find_text_location.return_value = LocatorResult(
            percentage=0.60,
            xpath="/body/DocFragment[1]/body/p[12]",
            match_index=1200,
        )
        mocks['ebook_parser'].get_perfect_ko_xpath.return_value = "/body/DocFragment[1]/body/p[12]"

        # The stale-id failure, exactly as ABSClient reports it: create_session
        # 404s, so update_progress_using_payload returns success=False.
        mocks['abs_client'].create_session.return_value = None
        mocks['abs_client'].update_progress.return_value = {
            "success": False,
            "code": None,
            "reason": "Failed to create ABS session for item test-abs-id-stale",
        }
        mocks['abs_client'].item_exists.return_value = item_exists

        transcriber = Mock()
        transcriber.get_text_at_time.return_value = "text near 60 percent"
        transcriber.find_time_for_text.return_value = 600.0

        from src.sync_manager import SyncManager
        from src.sync_clients.kosync_sync_client import KoSyncSyncClient
        from src.sync_clients.storyteller_sync_client import StorytellerSyncClient
        from src.sync_clients.booklore_sync_client import BookloreSyncClient

        manager = SyncManager(
            abs_client=mocks['abs_client'],
            booklore_client=mocks['booklore_client'],
            transcriber=transcriber,
            ebook_parser=mocks['ebook_parser'],
            database_service=mocks['database_service'],
            sync_clients={
                "ABS": ABSSyncClient(mocks['abs_client'], transcriber, mocks['ebook_parser']),
                "ABS eBook": ABSEbookSyncClient(mocks['abs_client'], mocks['ebook_parser']),
                "KoSync": KoSyncSyncClient(mocks['kosync_client'], mocks['ebook_parser']),
                "Storyteller": StorytellerSyncClient(mocks['storyteller_client'], mocks['ebook_parser']),
                "BookLore": BookloreSyncClient(mocks['booklore_client'], mocks['ebook_parser']),
            },
            epub_cache_dir=Path(self.temp_dir) / 'epub_cache',
            data_dir=Path(self.temp_dir),
            books_dir=Path(self.temp_dir) / 'books',
        )
        manager._automatch_hardcover = Mock()
        manager._sync_to_hardcover = Mock()
        manager._get_local_epub = Mock(
            return_value=str(Path(self.temp_dir) / 'books' / 'test-book.epub')
        )
        return manager, mocks

    def _run_cycle_capturing_logs(self, item_exists, claimants=None, claimants_error=None):
        from io import StringIO

        manager, mocks = self._build_manager(item_exists)
        # Default to a single claimant: the shared catalog row may only be demoted
        # when nobody else still claims the book.
        if claimants_error is not None:
            mocks['database_service'].get_book_user_ids.side_effect = claimants_error
        else:
            mocks['database_service'].get_book_user_ids.return_value = (
                [1] if claimants is None else claimants
            )

        log_stream = StringIO()
        handler = logging.StreamHandler(log_stream)
        root = logging.getLogger()
        original_level = root.level
        root.setLevel(logging.INFO)
        root.addHandler(handler)
        try:
            manager.sync_cycle()
        finally:
            root.removeHandler(handler)
            root.setLevel(original_level)

        return mocks, log_stream.getvalue()

    def test_confirmed_missing_item_marks_book_error(self):
        mocks, log_output = self._run_cycle_capturing_logs(item_exists=False)

        mocks['database_service'].set_book_status.assert_called_once_with(
            'test-abs-id-stale', 'error'
        )
        self.assertIn(
            "🛑 'test-abs-id-stale' 'Stale Mapping Test Book' Audiobookshelf library item no longer exists",
            log_output,
        )
        self.assertIn("marking book as 'error'", log_output)

    def test_confirmed_missing_item_skips_state_persistence(self):
        mocks, _ = self._run_cycle_capturing_logs(item_exists=False)

        self.assertFalse(
            mocks['database_service'].save_state.called,
            "A book being marked 'error' must not also record a partial sync",
        )

    def test_shared_book_is_not_demoted_for_one_users_stale_mapping(self):
        """Book.status is catalog-wide, but the 404 probe only speaks for one user.

        Demoting a book two people claim would stop it syncing for the second user
        because the first user's Audiobookshelf lost the item (CLAUDE.md failure
        mode #5: per-user state must not leak into shared catalog state).
        """
        mocks, log_output = self._run_cycle_capturing_logs(
            item_exists=False, claimants=[1, 2]
        )

        mocks['database_service'].set_book_status.assert_not_called()
        self.assertIn("Audiobookshelf library item no longer exists", log_output)
        self.assertIn("stays active for the others", log_output)

    def test_single_claimant_book_is_still_demoted(self):
        mocks, log_output = self._run_cycle_capturing_logs(
            item_exists=False, claimants=[7]
        )

        mocks['database_service'].set_book_status.assert_called_once_with(
            'test-abs-id-stale', 'error'
        )
        self.assertIn("marking book as 'error'", log_output)

    def test_unresolvable_claimants_leave_book_active(self):
        """An unknown claimant count must not be treated as 'single claimant'.

        The demotion guard reads the claimant list to decide whether the shared
        catalog row is safe to touch. If that read fails, the answer is unknown —
        and demoting on unknown reproduces the exact cross-user breakage the guard
        was added to prevent.
        """
        mocks, log_output = self._run_cycle_capturing_logs(
            item_exists=False, claimants_error=RuntimeError("database is locked")
        )

        mocks['database_service'].set_book_status.assert_not_called()
        self.assertIn("claimants could not be resolved", log_output)
        self.assertIn("leaving the book active", log_output)

    def test_indeterminate_probe_leaves_book_active(self):
        mocks, log_output = self._run_cycle_capturing_logs(item_exists=None)

        mocks['database_service'].set_book_status.assert_not_called()
        self.assertNotIn("Audiobookshelf library item no longer exists", log_output)

    def test_reachable_item_leaves_book_active(self):
        mocks, log_output = self._run_cycle_capturing_logs(item_exists=True)

        mocks['database_service'].set_book_status.assert_not_called()
        self.assertNotIn("Audiobookshelf library item no longer exists", log_output)


if __name__ == '__main__':
    unittest.main(verbosity=2)
