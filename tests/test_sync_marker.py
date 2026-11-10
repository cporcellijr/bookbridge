"""The library card glows the leader of the last sync that reached a follower.

Regression: an audio-led sync logged "BookOrbitAudio leads at 13.6029%" and
updated the ebook and KoSync, yet the card kept glowing KOReader, because the
glow read the newest *completed reading session* (the previous night's
BridgeSync_Plugin session) while the audio session was still open. The sync
cycle now records its own marker, and only when a follower write landed.
"""

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.base_sync_test import BaseSyncCycleTestCase
import tests.test_abs_stale_item_marks_error as stale_item
from src.sync_clients.abs_sync_client import ABSSyncClient
from src.sync_clients.booklore_sync_client import BookloreSyncClient
from src.sync_clients.sync_client_interface import SyncResult


class TestSyncCycleRecordsMarker(BaseSyncCycleTestCase):
    """KoSync leads at 60%; the followers' write outcomes decide the marker."""

    get_test_mapping = stale_item.TestStaleABSItemMarksBookError.get_test_mapping
    get_test_state_data = stale_item.TestStaleABSItemMarksBookError.get_test_state_data
    get_expected_leader = stale_item.TestStaleABSItemMarksBookError.get_expected_leader
    get_expected_final_percentage = stale_item.TestStaleABSItemMarksBookError.get_expected_final_percentage
    get_progress_mock_returns = stale_item.TestStaleABSItemMarksBookError.get_progress_mock_returns
    _build_manager = stale_item.TestStaleABSItemMarksBookError._build_manager

    def _cycle(self, abs_result, booklore_result=None, no_leader=False):
        manager, mocks = self._build_manager(item_exists=True)
        if no_leader:
            # _determine_leader's "nothing to do" answer: no change, own write-back, deadband.
            manager._determine_leader = lambda *args, **kwargs: (None, None)
        booklore_result = booklore_result or SyncResult(None, False)
        with patch.object(ABSSyncClient, 'update_progress', return_value=abs_result), \
                patch.object(BookloreSyncClient, 'update_progress', return_value=booklore_result):
            manager.sync_cycle()
        return mocks['database_service']

    def test_a_landed_follower_write_records_the_leader(self):
        db = self._cycle(SyncResult(600.0, True))
        db.record_sync_marker.assert_called_once()
        abs_id, leader, synced_at = db.record_sync_marker.call_args.args
        self.assertEqual((abs_id, leader), ('test-abs-id-stale', 'KoSync'))
        self.assertGreater(synced_at, 0)

    def test_partial_failure_still_records_the_leader(self):
        db = self._cycle(SyncResult(None, False), booklore_result=SyncResult(0.6, True))
        db.record_sync_marker.assert_called_once()
        self.assertEqual(db.record_sync_marker.call_args.args[1], 'KoSync')

    def test_failed_only_cycle_leaves_the_marker(self):
        db = self._cycle(SyncResult(None, False))
        db.save_state.assert_called()  # the leader's own state still saves
        db.record_sync_marker.assert_not_called()

    def test_superseded_only_cycle_leaves_the_marker(self):
        # A skipped result is a policy no-write (e.g. a newer remote position won).
        db = self._cycle(SyncResult(600.0, True, skipped=True), booklore_result=SyncResult(0.6, True, skipped=True))
        db.record_sync_marker.assert_not_called()

    def test_no_change_cycle_leaves_the_marker(self):
        db = self._cycle(SyncResult(600.0, True), no_leader=True)
        db.save_state.assert_not_called()
        db.record_sync_marker.assert_not_called()


class TestSyncMarkerStorage(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = str(Path(self.temp_dir) / 'test_database.db')
        from src.db.database_service import DatabaseService
        from src.db.models import Book
        self.DatabaseService = DatabaseService
        self.db = DatabaseService(self.db_path)
        self.alice = self.db.create_user("alice", "pw-alice", role="admin")
        self.bob = self.db.create_user("bob", "pw-bob", role="user")
        self.db.save_book(Book(abs_id="shared", abs_title="Shared", ebook_filename="s.epub", status="active"))

    def tearDown(self):
        self.db.db_manager.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_users_sharing_a_book_keep_separate_markers(self):
        self.db.record_sync_marker("shared", "BookOrbitAudio", 200.0, user_id=self.alice.id)
        self.db.record_sync_marker("shared", "KoSync", 100.0, user_id=self.bob.id)
        self.assertEqual(self.db.get_sync_markers(user_id=self.alice.id), {"shared": ("BookOrbitAudio", 200.0)})
        self.assertEqual(self.db.get_sync_markers(user_id=self.bob.id), {"shared": ("KoSync", 100.0)})

    def test_newer_sync_replaces_the_marker_and_survives_reopening(self):
        self.db.record_sync_marker("shared", "BookOrbitAudio", 200.0, user_id=self.alice.id)
        self.db.record_sync_marker("shared", "KoSync", 300.0, user_id=self.alice.id)
        self.db.db_manager.close()
        self.db = self.DatabaseService(self.db_path)
        self.assertEqual(self.db.get_sync_markers(user_id=self.alice.id), {"shared": ("KoSync", 300.0)})

    def test_deleting_the_book_drops_its_markers(self):
        self.db.record_sync_marker("shared", "KoSync", 100.0, user_id=self.bob.id)
        self.db.delete_book("shared")
        self.assertEqual(self.db.get_sync_markers(user_id=self.bob.id), {})


if __name__ == '__main__':
    unittest.main()
