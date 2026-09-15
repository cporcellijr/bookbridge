#!/usr/bin/env python3
"""
Regression tests for issue #358 ("Can't reset progress?").

The reporter was re-reading Dungeon Runner Dana. ABS held the correct audio
position (~76.9%) and was the acknowledged leader, but KoSync, Hardcover and
StoryGraph were all pinned at 100%, and every progress reset came straight back.
Their log showed BookBridge writing the 100% itself, through its own bot device:

    KOSync: Received progress from 'abs-sync-bot' for doc
    6453d1af20fab7ae4ccd9d1b6f52b09a -> 100.00% (Updated from 100.00%)

`_iter_update_targets` excludes the leader, so a write *to* KoSync proves KoSync
was not leading — ABS was. The ABS timestamp resolved to a locator at ~100%
(an alignment map running off the end of the text), and nothing caught it:
`_locator_collapsed_to_start` guards the 0% direction only. The 100% was pushed
to every follower, which marked the book finished everywhere and re-asserted
itself on the cycle after each reset.

`_locator_collapsed_to_end` is the mirror guard. A genuine finish must still
propagate, so these tests pin both directions.
"""

import logging
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.base_sync_test import BaseSyncCycleTestCase
from src.sync_clients.sync_client_interface import LocatorResult


class TestLocatorCollapsedToEndPredicate(unittest.TestCase):
    """Only a materially-behind leader may trip the end-collapse guard."""

    def setUp(self):
        from src.sync_manager import SyncManager
        self.collapsed = SyncManager._locator_collapsed_to_end

    def test_reporter_case_is_collapsed(self):
        # ABS at 76.9% resolving to 100% — the exact #358 shape.
        self.assertTrue(self.collapsed(LocatorResult(percentage=1.0), 0.769))

    def test_genuine_finish_is_not_collapsed(self):
        self.assertFalse(
            self.collapsed(LocatorResult(percentage=1.0), 1.0),
            "A finished leader resolving to 100% is correct, not a collapse",
        )

    def test_near_finish_within_margin_is_not_collapsed(self):
        # Stopping before the credits still has to be able to complete the book.
        self.assertFalse(self.collapsed(LocatorResult(percentage=1.0), 0.97))
        # Exactly at the margin is deliberately allowed through: the guard blocks
        # a write, so ties resolve in favour of syncing.
        self.assertFalse(self.collapsed(LocatorResult(percentage=1.0), 0.95))

    def test_just_outside_margin_is_collapsed(self):
        self.assertTrue(self.collapsed(LocatorResult(percentage=1.0), 0.9499))

    def test_mid_book_locator_is_not_collapsed(self):
        # Only the ~100% signature counts; ordinary disagreement is not this guard's job.
        self.assertFalse(self.collapsed(LocatorResult(percentage=0.80), 0.50))

    def test_start_collapse_is_not_confused_for_end(self):
        self.assertFalse(self.collapsed(LocatorResult(percentage=0.0), 0.769))

    def test_missing_values_are_safe(self):
        self.assertFalse(self.collapsed(None, 0.5))
        self.assertFalse(self.collapsed(LocatorResult(percentage=None), 0.5))
        self.assertFalse(self.collapsed(LocatorResult(percentage=1.0), None))


class TestEndCollapseGuardSyncCycle(BaseSyncCycleTestCase):
    """Integration: a mid-book ABS leader must not mark followers finished."""

    def get_test_mapping(self):
        return {
            'abs_id': 'test-abs-id-end-collapse',
            'abs_title': 'Dungeon Runner Dana',
            'kosync_doc_id': '6453d1af20fab7ae4ccd9d1b6f52b09a',
            'ebook_filename': 'test-book.epub',
            'transcript_file': str(Path(self.temp_dir) / 'test_transcript.json'),
            'status': 'active',
            'duration': 35516.0,
        }

    def get_test_state_data(self):
        # ABS moved forward this cycle; KoSync has not been touched.
        return {
            'abs': {'pct': 0.50, 'ts': 17758.0, 'last_updated': 1234567890},
            'kosync': {'pct': 0.50, 'last_updated': 1234567890},
        }

    def get_expected_leader(self):
        return "ABS"

    def get_expected_final_percentage(self):
        return 0.769

    def get_progress_mock_returns(self):
        return {
            # 7:35:12 of a 9h52m book — the reporter's dashboard value.
            'abs_progress': {'currentTime': 27312.0, 'duration': 35516.0},
            'abs_in_progress': [
                {'id': 'test-abs-id-end-collapse', 'progress': 0.769, 'duration': 35516.0}
            ],
            'kosync_progress': (0.50, "/body/DocFragment[1]/body/p[1]"),
            'storyteller_progress': (0.0, 0.0, None, None),
            'booklore_progress': (0.0, None),
        }

    def _build_manager(self, resolved_pct):
        """Real SyncManager with mocked clients; every text->locator resolution
        comes back at `resolved_pct` regardless of where the leader actually is."""
        mocks = self.setup_common_mocks()

        mocks['ebook_parser'].resolve_xpath.return_value = "text near the end"
        mocks['ebook_parser'].get_text_at_percentage.return_value = "text near the end"
        mocks['ebook_parser'].find_text_location.return_value = LocatorResult(
            percentage=resolved_pct,
            xpath="/body/DocFragment[42]/body/p[7]",
            match_index=999_000,
        )
        mocks['ebook_parser'].get_perfect_ko_xpath.return_value = "/body/DocFragment[42]/body/p[7]"
        # KoSync writes derive their own block-level XPointer; give it a valid one
        # so a faithful locator actually reaches kosync_client.update_progress.
        mocks['ebook_parser'].get_sentence_level_ko_xpath.return_value = "/body/DocFragment[42]/body/p[7]"

        transcriber = Mock()
        transcriber.get_text_at_time.return_value = "text near the end"
        transcriber.find_time_for_text.return_value = 27312.0

        from src.sync_manager import SyncManager
        from src.sync_clients.abs_sync_client import ABSSyncClient
        from src.sync_clients.abs_ebook_sync_client import ABSEbookSyncClient
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

    def _run_cycle_capturing_logs(self, resolved_pct):
        from io import StringIO

        manager, mocks = self._build_manager(resolved_pct)

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

    def test_collapsed_end_locator_is_not_written_to_kosync(self):
        mocks, log_output = self._run_cycle_capturing_logs(resolved_pct=1.0)

        # The reporter's symptom: BookBridge PUTs 100% to its own KoSync server.
        self.assertFalse(
            mocks['kosync_client'].update_progress.called,
            "KoSync was written a 100% completion resolved from a leader at 76.9%",
        )
        self.assertIn("Resolved locator collapsed to end-of-book (100%)", log_output)
        self.assertIn("skipping cross-client write to avoid marking the book finished", log_output)

    def test_collapsed_end_locator_does_not_complete_other_clients(self):
        mocks, _ = self._run_cycle_capturing_logs(resolved_pct=1.0)

        self.assertFalse(
            mocks['booklore_client'].update_progress.called,
            "Grimmory was marked finished from a collapsed end-of-book locator",
        )
        self.assertFalse(
            mocks['storyteller_client'].update_position.called,
            "Storyteller was marked finished from a collapsed end-of-book locator",
        )

    def test_collapsed_end_locator_records_leader_snapshot(self):
        mocks, _ = self._run_cycle_capturing_logs(resolved_pct=1.0)

        saved_abs = [
            call.args[0]
            for call in mocks['database_service'].save_state.call_args_list
            if getattr(call.args[0], 'client_name', None) == 'abs'
        ]
        self.assertTrue(saved_abs, "Leader (ABS) snapshot was not persisted")
        self.assertAlmostEqual(float(saved_abs[-1].percentage), 0.769, places=2)

    def test_faithful_locator_still_syncs(self):
        """The guard must not block an honest mid-book resolution."""
        mocks, log_output = self._run_cycle_capturing_logs(resolved_pct=0.769)

        self.assertNotIn("collapsed to end-of-book", log_output)
        self.assertTrue(
            mocks['kosync_client'].update_progress.called,
            "A faithful 76.9% locator must still be written to KoSync",
        )


if __name__ == '__main__':
    unittest.main(verbosity=2)
