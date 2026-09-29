import os
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.db.models import GoodreadsDetails
from src.sync_clients.goodreads_sync_client import GoodreadsSyncClient
from src.sync_clients.sync_client_interface import LocatorResult, SyncResult, UpdateProgressRequest
from src.sync_manager import SyncManager, _COMPLETION_PROPAGATION_EXCLUDED_CLIENTS
from src.services.reading_position_preview import _WRITE_ONLY_CLIENTS


class TestGoodreadsSyncClient(unittest.TestCase):
    def setUp(self):
        self.book = SimpleNamespace(
            abs_id="a1", abs_title="Example Book", ebook_filename="example.epub",
            original_ebook_filename="example.epub",
        )
        self.client = Mock()
        self.client.is_configured.return_value = True
        self.client.book_url.side_effect = lambda book_id: f"https://www.goodreads.com/book/show/{book_id}"
        self.client.set_shelf.return_value = True
        self.client.update_progress.return_value = True
        self.client.get_book.return_value = {
            "book_id": "123", "title": "Example Book", "author": "Example Author",
            "pages": 320, "rating": 4.2, "review_count": 120,
        }
        self.abs_client = Mock()
        self.abs_client.get_item_details.return_value = {
            "media": {"metadata": {
                "title": "Example Book", "authorName": "Example Author", "isbn": "9780123456789",
            }}
        }
        self.booklore = Mock()
        self.booklore.find_book_by_filename.return_value = {"metadata": {"goodreadsId": 123}}
        self.db = Mock()
        self.db.get_goodreads_details.return_value = None
        self.db.save_goodreads_details.side_effect = lambda details: details
        self.sync = GoodreadsSyncClient(
            self.client, Mock(), self.abs_client, self.db, booklore_client=self.booklore,
        )
        self.llm_off = patch("src.sync_clients.goodreads_sync_client.tracker_match_enabled", return_value=False)
        self.llm_off.start()
        self.addCleanup(self.llm_off.stop)

    def test_grimmory_id_matches_before_search_without_shelving(self):
        details = self.sync._automatch(self.book)
        self.assertEqual((details.goodreads_book_id, details.matched_by), ("123", "grimmory_id"))
        self.assertEqual(details.goodreads_pages, 320)
        self.assertEqual(details.goodreads_rating, 4.2)
        self.client.search_books.assert_not_called()
        self.client.set_shelf.assert_not_called()
        self.booklore.find_book_by_filename.assert_called_once_with("example.epub", allow_refresh=False)

    def test_isbn_then_title_author_and_author_gate(self):
        self.booklore.find_book_by_filename.return_value = {}
        self.client.search_books.side_effect = [
            [],
            [{"book_id": "123", "title": "Example Book", "author": "Wrong Person"}],
        ]
        self.assertIsNone(self.sync._automatch(self.book))
        self.assertEqual([call.args[0] for call in self.client.search_books.call_args_list],
                         ["9780123456789", "Example Book Example Author"])
        self.db.save_goodreads_details.assert_not_called()
        self.client.search_books.reset_mock()
        self.client.search_books.side_effect = [
            [], [{"book_id": "123", "title": "Example Book", "author": "Example Author"}],
        ]
        details = self.sync._automatch(self.book)
        self.assertEqual(details.matched_by, "title_author")

    def test_title_automatch_rejects_collection_with_same_author(self):
        self.booklore.find_book_by_filename.return_value = {}
        self.sync._match_metadata = Mock(return_value=("Dungeon Crawler Carl", "Matt Dinniman", "", ""))
        self.client.search_books.return_value = [{
            "book_id": "123", "title": "Dungeon Crawler Carl Series by Matt Dinniman 3 Books Collection Set",
            "author": "Matt Dinniman",
        }]
        self.assertIsNone(self.sync._automatch(self.book))
        self.db.save_goodreads_details.assert_not_called()

    def test_saved_link_posts_shelf_thresholds_and_progress(self):
        self.db.get_goodreads_details.return_value = GoodreadsDetails("a1", "123")
        for fraction, shelf in ((0.02, "to-read"), (0.03, "currently-reading"), (0.99, "read")):
            with self.subTest(fraction=fraction):
                result = self.sync.update_progress(
                    self.book, UpdateProgressRequest(LocatorResult(percentage=fraction)))
                self.assertTrue(result.success)
                self.client.set_shelf.assert_called_with("123", shelf)
                self.client.update_progress.assert_called_with("123", fraction)
        self.client.search_books.assert_not_called()

    def test_goodreads_never_leads_or_propagates_completion(self):
        self.assertFalse(self.sync.can_be_leader())
        self.assertIsNone(self.sync.get_service_state(self.book, None))
        self.assertIn("Goodreads", _COMPLETION_PROPAGATION_EXCLUDED_CLIENTS)
        self.assertIn("goodreads", _WRITE_ONLY_CLIENTS)


class TestGoodreadsCooldown(unittest.TestCase):
    def test_zero_progress_resets_previous_goodreads_post(self):
        manager = SyncManager.__new__(SyncManager)
        tracker = Mock()
        tracker.is_configured.return_value = True
        tracker.update_progress.return_value = SyncResult(0, True)
        manager.sync_clients = {"Goodreads": tracker}
        manager.database_service = Mock()
        manager.database_service.get_state.return_value = SimpleNamespace(percentage=1.0)
        manager._goodreads_cooldown = {}
        manager._goodreads_cooldown_lock = threading.Lock()
        book = SimpleNamespace(abs_id="a1")
        config = {"ABS": SimpleNamespace(current={"pct": 0.0})}
        with patch.dict(os.environ, {"GOODREADS_UPDATE_COOLDOWN_MINS": "0"}):
            manager._handle_goodreads_cooldown(book, config, 0, schedule_followup=False)
        tracker.update_progress.assert_called_once()
        self.assertEqual(manager.database_service.save_state.call_args.args[0].percentage, 0)
        manager.database_service.get_state.return_value = None
        tracker.update_progress.reset_mock()
        manager._handle_goodreads_cooldown(book, config, 1, schedule_followup=False)
        tracker.update_progress.assert_not_called()

    def test_posts_after_idle_and_immediately_at_completion(self):
        manager = SyncManager.__new__(SyncManager)
        tracker = Mock()
        tracker.is_configured.return_value = True
        tracker.update_progress.return_value = SyncResult(0.5, True)
        manager.sync_clients = {"Goodreads": tracker}
        manager.database_service = Mock()
        manager.database_service.get_state.return_value = None
        manager._goodreads_cooldown = {}
        manager._goodreads_cooldown_lock = threading.Lock()
        book = SimpleNamespace(abs_id="a1")
        config = {"ABS": SimpleNamespace(current={"pct": 0.5})}
        with patch.dict(os.environ, {"GOODREADS_UPDATE_COOLDOWN_MINS": "60"}):
            manager._handle_goodreads_cooldown(book, config, 0, schedule_followup=False)
            tracker.update_progress.assert_not_called()
            manager._handle_goodreads_cooldown(book, config, 3600, schedule_followup=False)
            self.assertEqual(tracker.update_progress.call_count, 1)
            saved = manager.database_service.save_state.call_args.args[0]
            self.assertEqual((saved.client_name, saved.percentage), ("goodreads", 0.5))
            config["ABS"].current["pct"] = 1.0
            manager._handle_goodreads_cooldown(book, config, 3601, schedule_followup=False)
            self.assertEqual(tracker.update_progress.call_count, 2)


if __name__ == "__main__":
    unittest.main()
