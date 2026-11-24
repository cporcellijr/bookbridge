"""A failed follower write is retried on later idle cycles; a failing bulk fetch is contained.

Regression: a follower write failed, the leader's state saved anyway, and every later
cycle saw delta 0 for every client and logged "Waiting for a new read event" forever,
leaving the follower behind until someone read again.
"""

import logging
from io import StringIO
from unittest.mock import Mock, patch

from tests.base_sync_test import BaseSyncCycleTestCase
import tests.test_abs_stale_item_marks_error as stale_item
from src.sync_clients.abs_sync_client import ABSSyncClient
from src.sync_clients.booklore_sync_client import BookloreSyncClient
from src.sync_clients.storyteller_sync_client import StorytellerSyncClient
from src.sync_clients.sync_client_interface import SyncResult
from src.sync_manager import _MAX_WRITE_RETRIES

ABS_ID = 'test-abs-id-stale'


class TestFollowerWriteRetry(BaseSyncCycleTestCase):
    get_test_mapping = stale_item.TestStaleABSItemMarksBookError.get_test_mapping
    get_expected_leader = stale_item.TestStaleABSItemMarksBookError.get_expected_leader
    get_expected_final_percentage = stale_item.TestStaleABSItemMarksBookError.get_expected_final_percentage
    get_progress_mock_returns = stale_item.TestStaleABSItemMarksBookError.get_progress_mock_returns
    _build_manager = stale_item.TestStaleABSItemMarksBookError._build_manager

    def get_test_state_data(self):
        # KoSync moved 10% -> 60% (see get_progress_mock_returns); the rest are unchanged.
        return {
            'abs': {'pct': 0.10, 'ts': 100.0, 'last_updated': 1234567890},
            'kosync': {'pct': 0.10, 'last_updated': 1234567890},
            'abs ebook': {'pct': 0.10, 'last_updated': 1234567890},
            'storyteller': {'pct': 0.0, 'last_updated': 1234567890},
            'booklore': {'pct': 0.0, 'last_updated': 1234567890},
        }

    def _settle_states(self):
        """Persisted state now equals what every client reports: delta 0, discrepancy kept."""
        for state in self.test_states:
            if state.client_name == 'kosync':
                state.percentage = 0.60

    def _cycle(self, manager, abs_result, booklore_result=None):
        booklore_result = booklore_result or SyncResult(0.6, True)
        with patch.object(ABSSyncClient, 'update_progress', return_value=abs_result) as abs_update, \
                patch.object(BookloreSyncClient, 'update_progress', return_value=booklore_result), \
                patch.object(StorytellerSyncClient, 'update_progress', return_value=SyncResult(0.6, True)):
            manager.sync_cycle()
        return abs_update

    def test_failed_follower_write_is_retried_on_an_idle_cycle(self):
        manager, _ = self._build_manager(item_exists=True)
        self.assertEqual(self._cycle(manager, SyncResult(None, False)).call_count, 1)
        self._settle_states()
        retry = self._cycle(manager, SyncResult(None, False))
        self.assertEqual(retry.call_count, 1, "idle cycle must push to the follower again")

    def test_success_clears_the_marker_and_the_next_idle_cycle_skips(self):
        manager, _ = self._build_manager(item_exists=True)
        self._cycle(manager, SyncResult(None, False))
        self._settle_states()
        self.assertEqual(self._cycle(manager, SyncResult(600.0, True)).call_count, 1)
        self.assertEqual(manager._write_retries, {})
        self.assertEqual(self._cycle(manager, SyncResult(600.0, True)).call_count, 0)

    def test_skipped_result_is_not_a_failure(self):
        manager, _ = self._build_manager(item_exists=True)
        self._cycle(manager, SyncResult(600.0, True, skipped=True))
        self.assertEqual(manager._write_retries, {})
        self._settle_states()
        self.assertEqual(self._cycle(manager, SyncResult(600.0, True, skipped=True)).call_count, 0)

    def test_retries_stop_after_the_cap(self):
        manager, _ = self._build_manager(item_exists=True)
        self._cycle(manager, SyncResult(None, False))
        self._settle_states()
        calls = [self._cycle(manager, SyncResult(None, False)).call_count
                 for _ in range(_MAX_WRITE_RETRIES + 2)]
        self.assertEqual(calls, [1] * _MAX_WRITE_RETRIES + [0, 0])

    def test_retry_decision_is_logged(self):
        manager, _ = self._build_manager(item_exists=True)
        self._cycle(manager, SyncResult(None, False))
        self._settle_states()
        stream = StringIO()
        handler = logging.StreamHandler(stream)
        root = logging.getLogger()
        level = root.level
        root.setLevel(logging.INFO)
        root.addHandler(handler)
        try:
            self._cycle(manager, SyncResult(None, False))
        finally:
            root.removeHandler(handler)
            root.setLevel(level)
        self.assertIn(f"🔁 '{ABS_ID}' 'Stale Mapping Test Book' Retrying after a failed write", stream.getvalue())

    def test_retry_records_the_leader_whose_write_failed(self):
        manager, _ = self._build_manager(item_exists=True)
        self._cycle(manager, SyncResult(None, False))
        self.assertEqual([entry["leader"] for entry in manager._write_retries.values()], ['KoSync'])

    def test_retry_is_dropped_when_another_client_would_lead(self):
        # Idle cycles fall back to furthest-wins; a retry must not re-push from a
        # client other than the one whose write failed (e.g. undoing a rewind).
        manager, _ = self._build_manager(item_exists=True)
        self._cycle(manager, SyncResult(None, False))
        self._settle_states()
        for entry in manager._write_retries.values():
            entry["leader"] = 'ABS'
        self.assertEqual(self._cycle(manager, SyncResult(None, False)).call_count, 0)
        self.assertEqual(manager._write_retries, {})

    def test_raising_bulk_fetch_does_not_stop_the_cycle(self):
        manager, _ = self._build_manager(item_exists=True)
        manager.sync_clients['Storyteller'].fetch_bulk_state = Mock(side_effect=ValueError("malformed"))
        abs_update = self._cycle(manager, SyncResult(600.0, True))
        self.assertEqual(abs_update.call_count, 1)
