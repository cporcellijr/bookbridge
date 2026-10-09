#!/usr/bin/env python3
"""
Regression tests for issue #486.

An ebook that never had an Audiobookshelf item was matched as ebook-only, so its
book key is the bridge-minted `ebook-<kosync hash>`. With "Enable ABS Ebook Sync"
on, the ABS ebook client read nothing for that key (404 → no state) but was still
handed the KOReader position to write. ABS 404'd the write, the existence probe
404'd too, and the sync cycle marked the book 'error' as a stale mapping:

    🛑 'ebook-e0262d5cceb927d0' 'Angel Down: A Novel - Daniel Kraus' Audiobookshelf
    library item no longer exists (reported by ABSEbook) — marking book as 'error'.
    Re-match it in the dashboard to resume syncing

Re-matching recreated the same key, so the book failed again on the next cycle.
"""

import logging
import os
import sys
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.base_sync_test import BaseSyncCycleTestCase
from src.db.models import Book
from src.sync_clients.abs_ebook_sync_client import ABSEbookSyncClient
from src.sync_clients.sync_client_interface import LocatorResult

REPORTED_ABS_ID = 'ebook-e0262d5cceb927d0'
REPORTED_TITLE = 'Angel Down: A Novel - Daniel Kraus'


class TestABSEbookSupportsBook(unittest.TestCase):
    """Only books that name a real ABS item reach the ABS ebook client."""

    def setUp(self):
        self.client = ABSEbookSyncClient(Mock(), Mock())

    def test_ebook_only_bridge_key_is_not_supported(self):
        book = Book(abs_id=REPORTED_ABS_ID, ebook_filename='angel.epub', sync_mode='ebook_only')
        self.assertFalse(self.client.supports_book(book))

    def test_match_queue_ebook_key_is_not_supported(self):
        book = Book(abs_id='ebook:some-key', ebook_filename='angel.epub')
        self.assertFalse(self.client.supports_book(book))

    def test_library_audiobook_bridge_keys_are_not_supported(self):
        for key in ('booklore:42', 'bookorbit:17', 'BookOrbit:17'):
            with self.subTest(key=key):
                book = Book(abs_id=key, ebook_filename='angel.epub')
                self.assertFalse(self.client.supports_book(book))

    def test_bridge_key_with_explicit_abs_ebook_item_is_supported(self):
        book = Book(abs_id=REPORTED_ABS_ID, ebook_filename='angel.epub',
                    abs_ebook_item_id='li_ebook_1')
        self.assertTrue(self.client.supports_book(book))

    def test_bridge_key_with_abs_ebook_source_is_supported(self):
        book = Book(abs_id='bookorbit:17', ebook_filename='angel.epub',
                    ebook_source='ABS', ebook_source_id='li_ebook_2')
        self.assertTrue(self.client.supports_book(book))

    def test_bridge_key_with_other_ebook_source_is_not_supported(self):
        book = Book(abs_id=REPORTED_ABS_ID, ebook_filename='angel.epub',
                    ebook_source='BookOrbit', ebook_source_id='99')
        self.assertFalse(self.client.supports_book(book))

    def test_real_abs_item_is_supported(self):
        # Legacy direct matches: the ABS item id is the book key.
        for mode in ('audiobook', 'ebook_only'):
            with self.subTest(sync_mode=mode):
                book = Book(abs_id='li_8f2c1e', ebook_filename='angel.epub', sync_mode=mode)
                self.assertTrue(self.client.supports_book(book))


class _EbookOnlyCycleCase(BaseSyncCycleTestCase):
    """The real sync cycle for an ebook-only book, with ABS ebook sync enabled."""

    abs_id = REPORTED_ABS_ID

    def setUp(self):
        self._original_sync_abs_ebook = os.environ.get('SYNC_ABS_EBOOK')
        os.environ['SYNC_ABS_EBOOK'] = 'true'
        super().setUp()
        self.test_book.sync_mode = 'ebook_only'
        self.test_book.transcript_file = None

    def tearDown(self):
        if self._original_sync_abs_ebook is None:
            os.environ.pop('SYNC_ABS_EBOOK', None)
        else:
            os.environ['SYNC_ABS_EBOOK'] = self._original_sync_abs_ebook
        super().tearDown()

    def get_test_mapping(self):
        return {
            'abs_id': self.abs_id,
            'abs_title': REPORTED_TITLE,
            'kosync_doc_id': 'e0262d5cceb927d0a1b2c3d4e5f60718',
            'ebook_filename': 'test-book.epub',
            'transcript_file': str(Path(self.temp_dir) / 'test_transcript.json'),
            'status': 'active',
        }

    def get_test_state_data(self):
        return {'kosync': {'pct': 0.10, 'last_updated': 1234567890}}

    def get_expected_leader(self):
        return "KoSync"

    def get_expected_final_percentage(self):
        return 0.60

    def get_progress_mock_returns(self):
        return {
            'abs_progress': None,
            'abs_in_progress': [],
            'kosync_progress': (0.60, "/body/DocFragment[1]/body/p[1]"),
            'storyteller_progress': (0.0, 0.0, None, None),
            'booklore_progress': (0.0, None),
        }

    def _run_cycle(self):
        mocks = self.setup_common_mocks()
        parser = mocks['ebook_parser']
        parser.resolve_xpath.return_value = "text near 60 percent"
        parser.get_text_at_percentage.return_value = "text near 60 percent"
        parser.find_text_location.return_value = LocatorResult(
            percentage=0.60,
            xpath="/body/DocFragment[1]/body/p[12]",
            cfi="epubcfi(/6/12!/4/2/1:0)",
            match_index=1200,
        )
        parser.get_perfect_ko_xpath.return_value = "/body/DocFragment[1]/body/p[12]"

        # What Audiobookshelf answers for an id it has never seen.
        abs_client = mocks['abs_client']
        abs_client.get_progress_with_status.return_value = (None, 404)
        abs_client.update_ebook_progress.return_value = False
        abs_client.item_exists.return_value = False
        mocks['database_service'].get_book_user_ids.return_value = [1]

        from src.sync_manager import SyncManager
        from src.sync_clients.kosync_sync_client import KoSyncSyncClient

        manager = SyncManager(
            abs_client=abs_client,
            booklore_client=mocks['booklore_client'],
            transcriber=Mock(),
            ebook_parser=parser,
            database_service=mocks['database_service'],
            sync_clients={
                "ABSEbook": ABSEbookSyncClient(abs_client, parser),
                "KoSync": KoSyncSyncClient(mocks['kosync_client'], parser),
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


class TestEbookOnlyBookWithoutABSItem(_EbookOnlyCycleCase):
    """Integration: the reporter's bridge-keyed ebook never reaches ABS."""

    def test_reported_book_is_not_marked_error(self):
        mocks, log_output = self._run_cycle()

        self.assertNotIn(
            f"🛑 '{REPORTED_ABS_ID}' '{REPORTED_TITLE}' Audiobookshelf library item no longer exists "
            f"(reported by ABSEbook) — marking book as 'error'",
            log_output,
        )
        mocks['database_service'].set_book_status.assert_not_called()

    def test_no_abs_request_is_made_for_a_bridge_key(self):
        mocks, _ = self._run_cycle()

        abs_client = mocks['abs_client']
        abs_client.get_progress_with_status.assert_not_called()
        abs_client.update_ebook_progress.assert_not_called()
        abs_client.item_exists.assert_not_called()


class TestEbookOnlyBookWithRealABSItem(_EbookOnlyCycleCase):
    """An ebook-only match to a real ABS ebook item keeps syncing to ABS."""

    abs_id = 'li_8f2c1e'

    def test_real_item_is_still_written(self):
        mocks, _ = self._run_cycle()

        mocks['abs_client'].update_ebook_progress.assert_called()
        written_id = mocks['abs_client'].update_ebook_progress.call_args[0][0]
        self.assertEqual(written_id, 'li_8f2c1e')


if __name__ == '__main__':
    unittest.main(verbosity=2)
