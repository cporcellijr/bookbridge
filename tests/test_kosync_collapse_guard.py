#!/usr/bin/env python3
"""
Integration test for the issue #290 follow-up fix.

Drives the real `SyncManager.sync_cycle()` end-to-end (mocked clients) for the
exact failure shape the reporter hit: a KoSync leader whose locator resolution
collapses to 0% (a no-longer-resolving XPath / out-of-range alignment timestamp).
The guard must skip the destructive write so ABS is NOT reset to start-of-book,
and must record the leader's own value so the static reading is not re-triggered.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.base_sync_test import BaseSyncCycleTestCase
from src.sync_clients.sync_client_interface import LocatorResult


class TestKoSyncCollapseGuardSync(BaseSyncCycleTestCase):
    """A collapsed KoSync locator must not stomp ABS to 0%."""

    def get_test_mapping(self):
        return {
            'abs_id': 'test-abs-id-collapse',
            'abs_title': 'Collapse Guard Test Book',
            'kosync_doc_id': 'test-kosync-doc-collapse',
            'ebook_filename': 'test-book.epub',
            'transcript_file': str(Path(self.temp_dir) / 'test_transcript.json'),
            'status': 'active',
        }

    def get_test_state_data(self):
        # KoSync's saved State has not yet "seen" the 53% sibling value, so it
        # reads as a fresh change this cycle — mirroring the reporter's logs.
        return {
            'abs': {'pct': 0.10, 'ts': 100.0, 'last_updated': 1234567890},
            'kosync': {'pct': 0.0, 'last_updated': 1234567890},
        }

    def get_expected_leader(self):
        return "KoSync"

    def get_expected_final_percentage(self):
        return 0.5314

    def get_progress_mock_returns(self):
        return {
            'abs_progress': {'currentTime': 100.0, 'duration': 1000},  # 10%
            'abs_in_progress': [{'id': 'test-abs-id-collapse', 'progress': 0.10, 'duration': 1000}],
            # 53.14% stale sibling value, the leader for this cycle.
            'kosync_progress': (0.5314, "/body/DocFragment[1]/body/p[1]"),
            'storyteller_progress': (0.0, 0.0, None, None),
            'booklore_progress': (0.0, None),
        }

    def _build_manager(self):
        """Build a real SyncManager with mocked clients (mirrors the base harness),
        but force the ebook locator resolution to collapse to 0%."""
        mocks = self.setup_common_mocks()

        # The crux: every text->locator resolution comes back at the start of the
        # book (0%) even though the KoSync leader is at 53% — the failed-resolution
        # collapse. resolve_xpath returns truthy text so we reach get_locator_from_text.
        mocks['ebook_parser'].resolve_xpath.return_value = "some text near 53%"
        mocks['ebook_parser'].get_text_at_percentage.return_value = "some text near 53%"
        mocks['ebook_parser'].find_text_location.return_value = LocatorResult(percentage=0.0)
        mocks['ebook_parser'].get_perfect_ko_xpath.return_value = None

        transcriber = Mock()
        transcriber.get_text_at_time.return_value = "text"
        transcriber.find_time_for_text.return_value = 531.4

        from src.sync_manager import SyncManager
        from src.sync_clients.abs_sync_client import ABSSyncClient
        from src.sync_clients.kosync_sync_client import KoSyncSyncClient
        from src.sync_clients.abs_ebook_sync_client import ABSEbookSyncClient
        from src.sync_clients.storyteller_sync_client import StorytellerSyncClient
        from src.sync_clients.booklore_sync_client import BookloreSyncClient

        abs_sync_client = ABSSyncClient(mocks['abs_client'], transcriber, mocks['ebook_parser'])
        kosync_sync_client = KoSyncSyncClient(mocks['kosync_client'], mocks['ebook_parser'])
        abs_ebook_sync_client = ABSEbookSyncClient(mocks['abs_client'], mocks['ebook_parser'])
        storyteller_sync_client = StorytellerSyncClient(mocks['storyteller_client'], mocks['ebook_parser'])
        booklore_sync_client = BookloreSyncClient(mocks['booklore_client'], mocks['ebook_parser'])

        manager = SyncManager(
            abs_client=mocks['abs_client'],
            booklore_client=mocks['booklore_client'],
            transcriber=transcriber,
            ebook_parser=mocks['ebook_parser'],
            database_service=mocks['database_service'],
            sync_clients={
                "ABS": abs_sync_client,
                "ABS eBook": abs_ebook_sync_client,
                "KoSync": kosync_sync_client,
                "Storyteller": storyteller_sync_client,
                "BookLore": booklore_sync_client,
            },
            epub_cache_dir=Path(self.temp_dir) / 'epub_cache',
            data_dir=Path(self.temp_dir),
            books_dir=Path(self.temp_dir) / 'books',
        )

        # Spy on the ABS write path — it must never fire on a collapse.
        manager.sync_clients['ABS']._update_abs_progress_with_offset = Mock(
            return_value=({"success": True}, 0.0)
        )
        manager._automatch_hardcover = Mock()
        manager._sync_to_hardcover = Mock()
        # Ensure a local epub path resolves so the cycle reaches the locator block.
        manager._get_local_epub = Mock(return_value=str(Path(self.temp_dir) / 'books' / 'test-book.epub'))
        return manager, mocks

    def test_collapsed_locator_does_not_reset_abs(self):
        manager, mocks = self._build_manager()

        manager.sync_cycle()

        # The destructive 0% write to ABS must NOT have happened.
        self.assertFalse(
            manager.sync_clients['ABS']._update_abs_progress_with_offset.called,
            "ABS was reset despite a collapsed (0%) locator — progress would be lost",
        )
        self.assertFalse(
            mocks['abs_client'].update_progress.called,
            "ABS update_progress should not be called on a collapsed locator",
        )

    def test_collapsed_locator_records_leader_snapshot(self):
        manager, mocks = self._build_manager()

        manager.sync_cycle()

        # The leader's own (static) value is persisted so it is not re-detected
        # as a fresh change next cycle.
        saved_kosync = [
            call.args[0]
            for call in mocks['database_service'].save_state.call_args_list
            if getattr(call.args[0], 'client_name', None) == 'kosync'
        ]
        self.assertTrue(saved_kosync, "Leader (KoSync) state snapshot was not persisted")
        self.assertAlmostEqual(float(saved_kosync[-1].percentage), 0.5314, places=4)


if __name__ == '__main__':
    unittest.main(verbosity=2)
