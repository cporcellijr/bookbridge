"""
Tests for ABSSocketListener debounce logic and KoSync PUT instant sync trigger.
"""

import threading
import time
import unittest
from unittest.mock import MagicMock, patch, PropertyMock

from src.services.abs_socket_listener import ABSSocketListener


class TestABSSocketListenerDebounce(unittest.TestCase):
    """Test the debounce logic in ABSSocketListener."""

    def setUp(self):
        """Create listener with mocked dependencies."""
        self.mock_db = MagicMock()
        self.mock_sync = MagicMock()

        with patch("src.services.abs_socket_listener.socketio.Client"):
            self.listener = ABSSocketListener(
                abs_server_url="http://abs.local:13378",
                abs_api_token="test-token",
                database_service=self.mock_db,
                sync_manager=self.mock_sync,
            )
        # Override debounce window to 1s for fast tests
        self.listener._debounce_window = 1

    def _make_active_book(self, abs_id: str, title: str = "Test Book", user_id=None):
        book = MagicMock()
        book.abs_id = abs_id
        book.abs_title = title
        book.status = "active"
        book.user_id = user_id
        return book

    def test_ignores_non_active_books(self):
        """Events for books not in DB or not active should be ignored."""
        # Book not in DB
        self.mock_db.get_book.return_value = None
        self.listener._handle_progress_event({"id": "prog-1", "data": {"libraryItemId": "unknown-id"}})

        self.assertEqual(len(self.listener._pending), 0)

        # Book exists but not active
        inactive = self._make_active_book("inactive-id")
        inactive.status = "pending"
        self.mock_db.get_book.return_value = inactive
        self.listener._handle_progress_event({"id": "prog-2", "data": {"libraryItemId": "inactive-id"}})

        self.assertEqual(len(self.listener._pending), 0)

    def test_records_active_book_event(self):
        """Events for active books should be recorded in pending dict."""
        book = self._make_active_book("book-1")
        self.mock_db.get_book.return_value = book

        self.listener._handle_progress_event({"id": "prog-3", "data": {"libraryItemId": "book-1"}})

        self.assertIn("book-1", self.listener._pending)

    def test_debounce_does_not_fire_before_window(self):
        """Sync should NOT fire if debounce window hasn't elapsed."""
        book = self._make_active_book("book-2")
        self.mock_db.get_book.return_value = book

        self.listener._handle_progress_event({"id": "prog-4", "data": {"libraryItemId": "book-2"}})
        self.listener._check_and_fire()

        self.mock_sync.sync_cycle.assert_not_called()

    def test_debounce_fires_after_window(self):
        """Sync SHOULD fire after debounce window elapses."""
        book = self._make_active_book("book-3", "Debounce Test")
        self.mock_db.get_book.return_value = book

        self.listener._handle_progress_event({"id": "prog-5", "data": {"libraryItemId": "book-3"}})

        # Simulate time passing
        self.listener._pending["book-3"] = time.time() - 2  # 2s ago, window is 1s
        self.listener._check_and_fire()

        # Give the daemon thread a moment
        time.sleep(0.1)
        self.mock_sync.sync_cycle.assert_called_once_with(target_abs_id="book-3", user_id=None)

    def test_no_double_fire(self):
        """Same event should not trigger sync twice."""
        book = self._make_active_book("book-4")
        self.mock_db.get_book.return_value = book

        self.listener._pending["book-4"] = time.time() - 2
        self.listener._check_and_fire()
        time.sleep(0.1)

        # First fire should have removed from pending
        self.assertEqual(len(self.listener._pending), 0)

        # Calling again should do nothing
        self.listener._check_and_fire()
        time.sleep(0.1)
        self.mock_sync.sync_cycle.assert_called_once()

    def test_new_event_after_fire_retriggers(self):
        """A new event after sync fired should start a fresh debounce."""
        book = self._make_active_book("book-5")
        self.mock_db.get_book.return_value = book

        # First event + fire
        self.listener._pending["book-5"] = time.time() - 2
        self.listener._check_and_fire()
        time.sleep(0.1)
        self.assertEqual(self.mock_sync.sync_cycle.call_count, 1)

        # New event
        self.listener._handle_progress_event({"id": "prog-6", "data": {"libraryItemId": "book-5"}})
        self.assertIn("book-5", self.listener._pending)

        # Fire again after window
        self.listener._pending["book-5"] = time.time() - 2
        self.listener._check_and_fire()
        time.sleep(0.1)
        self.assertEqual(self.mock_sync.sync_cycle.call_count, 2)

    def test_handles_nested_data_format(self):
        """Should handle the real ABS event format: {id, sessionId, data: {libraryItemId}}."""
        book = self._make_active_book("nested-id")
        self.mock_db.get_book.return_value = book

        self.listener._handle_progress_event({
            "id": "34621755-32df-4876-b235-abc123",
            "sessionId": "session-1",
            "deviceDescription": "Windows 10 / Firefox",
            "data": {"libraryItemId": "nested-id", "progress": 0.42}
        })
        self.assertIn("nested-id", self.listener._pending)

    def test_handles_top_level_library_item_id(self):
        """Should handle older ABS format with top-level libraryItemId."""
        book = self._make_active_book("top-level-id")
        self.mock_db.get_book.return_value = book

        self.listener._handle_progress_event({"libraryItemId": "top-level-id"})
        self.assertIn("top-level-id", self.listener._pending)

    def test_handles_missing_library_item_id(self):
        """Should silently ignore events with no libraryItemId."""
        self.listener._handle_progress_event({"someOtherField": "value"})
        self.assertEqual(len(self.listener._pending), 0)
        self.mock_db.get_book.assert_not_called()

    def test_url_stripping(self):
        """Server URL should strip trailing /api for socket connection."""
        with patch("src.services.abs_socket_listener.socketio.Client"):
            listener = ABSSocketListener(
                abs_server_url="http://abs.local:13378/api",
                abs_api_token="tok",
                database_service=MagicMock(),
                sync_manager=MagicMock(),
            )
        self.assertEqual(listener._server_url, "http://abs.local:13378")

    def test_per_user_listener_fires_scoped_sync_cycle(self):
        """A listener bound to a user_id triggers that user's scoped sync cycle."""
        with patch("src.services.abs_socket_listener.socketio.Client"):
            listener = ABSSocketListener(
                abs_server_url="http://abs.local:13378",
                abs_api_token="caitlin-token",
                database_service=self.mock_db,
                sync_manager=self.mock_sync,
                user_id=7,
            )
        listener._debounce_window = 1
        book = self._make_active_book("crawler-carl", "Dungeon Crawler Carl")
        self.mock_db.get_book.return_value = book

        listener._pending["crawler-carl"] = time.time() - 2
        listener._check_and_fire()

        time.sleep(0.1)
        self.mock_sync.sync_cycle.assert_called_once_with(
            target_abs_id="crawler-carl", user_id=7
        )

    def test_global_listener_fires_under_book_owner(self):
        """The global listener (user_id=None) scopes the cycle to the book's
        owner so its pushes share the per-user poller's suppression namespace."""
        book = self._make_active_book("owned-book", "Owned Book", user_id=3)
        self.mock_db.get_book.return_value = book

        self.listener._pending["owned-book"] = time.time() - 2
        self.listener._check_and_fire()

        time.sleep(0.1)
        self.mock_sync.sync_cycle.assert_called_once_with(
            target_abs_id="owned-book", user_id=3
        )

    def test_global_listener_suppresses_owner_scoped_self_write(self):
        """A self-write recorded under the resolved owner is suppressed by the
        global listener (the is_own_write check uses the same owner namespace)."""
        from src.services import write_tracker

        with write_tracker._writes_lock:
            write_tracker._recent_writes.clear()
        write_tracker.record_write("ABS", "owned-book", user_id=3)

        book = self._make_active_book("owned-book", "Owned Book", user_id=3)
        self.mock_db.get_book.return_value = book

        self.listener._pending["owned-book"] = time.time() - 2
        self.listener._check_and_fire()

        time.sleep(0.1)
        self.mock_sync.sync_cycle.assert_not_called()

    def test_global_listener_fans_out_to_all_claimants(self):
        """The global listener fires for EVERY user that claimed a shared book,
        not just the single owner column — otherwise co-claimants on the shared
        ABS token wait for the slow poll."""
        from src.services import write_tracker

        with write_tracker._writes_lock:
            write_tracker._recent_writes.clear()

        book = self._make_active_book("shared-book", "Shared Book", user_id=3)
        self.mock_db.get_book.return_value = book
        self.mock_db.get_book_user_ids.return_value = [3, 5]

        self.listener._pending["shared-book"] = time.time() - 2
        self.listener._check_and_fire()

        time.sleep(0.1)
        fired = {
            (c.kwargs["target_abs_id"], c.kwargs["user_id"])
            for c in self.mock_sync.sync_cycle.call_args_list
        }
        self.assertEqual(fired, {("shared-book", 3), ("shared-book", 5)})


class TestKosyncPutInstantSync(unittest.TestCase):
    """Test that KoSync PUT records debounce events for active linked books.

    Sync fires via _kosync_debounce_loop (background thread). These tests verify
    that events are correctly recorded in (or excluded from) the debounce queue —
    the actual fire timing is handled by the debounce loop, not tested here.
    """

    def setUp(self):
        import os
        os.environ.setdefault('DATA_DIR', '/tmp/test_kosync_instant')
        os.environ.setdefault('KOSYNC_USER', 'testuser')
        os.environ.setdefault('KOSYNC_KEY', 'testpass')
        os.environ['INSTANT_SYNC_ENABLED'] = 'true'

        import src.api.kosync_server as ks
        # Reset debounce state so each test starts clean
        ks._debounce_thread_started = False
        with ks._kosync_debounce_lock:
            ks._kosync_debounce.clear()

    def tearDown(self):
        import os
        os.environ.pop('INSTANT_SYNC_ENABLED', None)

    def _make_put_context(self, doc_hash, percentage=0.55, device='TestDevice'):
        from flask import Flask
        app = Flask(__name__)
        return app.test_request_context(
            '/syncs/progress',
            method='PUT',
            json={
                'document': doc_hash,
                'percentage': percentage,
                'progress': '/body/test',
                'device': device,
                'device_id': 'D1',
            },
            content_type='application/json',
        )

    def test_put_records_debounce_event_for_active_linked_book(self):
        """PUT for a linked active book should record a debounce event."""
        import src.api.kosync_server as ks
        from src.db.models import KosyncDocument

        mock_db = MagicMock()
        mock_db.get_user_kosync_progress.return_value = None
        original_db = ks._database_service
        ks._database_service = mock_db
        original_manager = ks._manager
        ks._manager = MagicMock()

        try:
            mock_book = MagicMock()
            mock_book.abs_id = "test-instant-sync"
            mock_book.abs_title = "Instant Sync Book"
            mock_book.status = "active"
            mock_book.kosync_doc_id = "x" * 32

            mock_doc = MagicMock(spec=KosyncDocument)
            mock_doc.linked_abs_id = "test-instant-sync"
            mock_doc.percentage = 0.3
            mock_doc.device_id = "D1"

            mock_db.get_kosync_document.return_value = mock_doc
            mock_db.get_book.return_value = mock_book
            mock_db.get_book_by_kosync_id.return_value = None

            with self._make_put_context('x' * 32):
                ks.kosync_put_progress.__wrapped__()

            # Event should be queued for the debounce loop to fire. The debounce
            # map is keyed by (abs_id, user_id); match on the stored abs_id.
            with ks._kosync_debounce_lock:
                entries = [v for v in ks._kosync_debounce.values() if v["abs_id"] == "test-instant-sync"]
                self.assertEqual(len(entries), 1)
                self.assertFalse(entries[0]["synced"])
                self.assertEqual(entries[0]["title"], "Instant Sync Book")

        finally:
            ks._database_service = original_db
            ks._manager = original_manager

    def test_instant_sync_disabled_skips_debounce(self):
        """PUT should NOT record a debounce event when INSTANT_SYNC_ENABLED=false."""
        import os
        import src.api.kosync_server as ks
        from src.db.models import KosyncDocument

        os.environ['INSTANT_SYNC_ENABLED'] = 'false'
        mock_db = MagicMock()
        mock_db.get_user_kosync_progress.return_value = None
        original_db = ks._database_service
        ks._database_service = mock_db
        original_manager = ks._manager
        ks._manager = MagicMock()

        try:
            mock_book = MagicMock()
            mock_book.abs_id = "test-disabled"
            mock_book.abs_title = "Disabled Book"
            mock_book.status = "active"
            mock_book.kosync_doc_id = "d" * 32

            mock_doc = MagicMock(spec=KosyncDocument)
            mock_doc.linked_abs_id = "test-disabled"
            mock_doc.percentage = 0.1
            mock_doc.device_id = "D1"

            mock_db.get_kosync_document.return_value = mock_doc
            mock_db.get_book.return_value = mock_book
            mock_db.get_book_by_kosync_id.return_value = None

            with self._make_put_context('d' * 32):
                ks.kosync_put_progress.__wrapped__()

            # No debounce event should have been recorded
            with ks._kosync_debounce_lock:
                self.assertFalse(any(v["abs_id"] == "test-disabled" for v in ks._kosync_debounce.values()))

        finally:
            ks._database_service = original_db
            ks._manager = original_manager

    def test_put_does_not_record_debounce_event_for_inactive_book(self):
        """PUT for a linked but inactive book should NOT record a debounce event."""
        import src.api.kosync_server as ks

        mock_db = MagicMock()
        mock_db.get_user_kosync_progress.return_value = None
        original_db = ks._database_service
        ks._database_service = mock_db
        original_manager = ks._manager
        ks._manager = MagicMock()

        try:
            mock_book = MagicMock()
            mock_book.abs_id = "test-inactive"
            mock_book.abs_title = "Inactive Book"
            mock_book.status = "pending"

            mock_doc = MagicMock()
            mock_doc.linked_abs_id = "test-inactive"
            mock_doc.percentage = 0.1
            mock_doc.device_id = "D1"

            mock_db.get_kosync_document.return_value = mock_doc
            mock_db.get_book.return_value = mock_book
            mock_db.get_book_by_kosync_id.return_value = None

            with self._make_put_context('y' * 32):
                ks.kosync_put_progress.__wrapped__()

            with ks._kosync_debounce_lock:
                self.assertFalse(any(v["abs_id"] == "test-inactive" for v in ks._kosync_debounce.values()))

        finally:
            ks._database_service = original_db
            ks._manager = original_manager

    def test_internal_put_clears_stale_debounce_entry(self):
        """Internal sync-bot PUT should clear any stale pending debounce event."""
        import src.api.kosync_server as ks
        from src.db.models import KosyncDocument

        mock_db = MagicMock()
        mock_db.get_user_kosync_progress.return_value = None
        original_db = ks._database_service
        ks._database_service = mock_db
        original_manager = ks._manager
        ks._manager = MagicMock()

        try:
            mock_book = MagicMock()
            mock_book.abs_id = "test-clear-stale"
            mock_book.abs_title = "Clear Stale Book"
            mock_book.status = "active"
            mock_book.kosync_doc_id = "z" * 32

            mock_doc = MagicMock(spec=KosyncDocument)
            mock_doc.linked_abs_id = "test-clear-stale"
            mock_doc.percentage = 0.4
            mock_doc.device_id = "reader-1"

            mock_db.get_kosync_document.return_value = mock_doc
            mock_db.get_book.return_value = mock_book
            mock_db.get_book_by_kosync_id.return_value = None

            with ks._kosync_debounce_lock:
                ks._kosync_debounce[("test-clear-stale", None)] = {
                    "last_event": time.time(),
                    "title": "Clear Stale Book",
                    "synced": False,
                    "user_id": None,
                    "abs_id": "test-clear-stale",
                }

            with self._make_put_context('z' * 32, percentage=0.0, device='abs-sync-bot'):
                ks.kosync_put_progress.__wrapped__()

            with ks._kosync_debounce_lock:
                self.assertFalse(any(v["abs_id"] == "test-clear-stale" for v in ks._kosync_debounce.values()))

        finally:
            ks._database_service = original_db
            ks._manager = original_manager

    def test_record_kosync_event_keys_per_user_for_shared_book(self):
        """Two users PUT-ing the same shared book must each get their own
        debounced sync — a bare abs_id key let the second overwrite the first."""
        import src.api.kosync_server as ks

        with ks._kosync_debounce_lock:
            ks._kosync_debounce.clear()
        started = ks._debounce_thread_started
        ks._debounce_thread_started = True  # suppress the real loop thread
        try:
            ks._record_kosync_event("shared-abs", "Shared", user_id=1)
            ks._record_kosync_event("shared-abs", "Shared", user_id=2)
            with ks._kosync_debounce_lock:
                users = {
                    v["user_id"]
                    for v in ks._kosync_debounce.values()
                    if v["abs_id"] == "shared-abs"
                }
            self.assertEqual(users, {1, 2})
        finally:
            ks._debounce_thread_started = started
            with ks._kosync_debounce_lock:
                ks._kosync_debounce.clear()


if __name__ == "__main__":
    unittest.main()
