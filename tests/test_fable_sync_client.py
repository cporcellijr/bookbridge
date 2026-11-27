"""Fable sync client: automatch and the list/progress writes for each reading position."""

import unittest
import unittest.mock
from types import SimpleNamespace
from unittest.mock import Mock

from src.db.models import FableDetails
from src.services.reading_position_preview import _WRITE_ONLY_CLIENTS
from src.sync_clients.fable_sync_client import FableSyncClient
from src.sync_clients.sync_client_interface import LocatorResult, UpdateProgressRequest
from src.sync_manager import _COMPLETION_PROPAGATION_EXCLUDED_CLIENTS

BOOK_ID = "0a1b2c3d-1111-4222-8333-444455556666"
YOURS = "9f8e7d6c-1111-4222-8333-444455556666"


def _request(fraction):
    return UpdateProgressRequest(locator_result=LocatorResult(percentage=fraction))


class TestFableSyncClient(unittest.TestCase):
    def setUp(self):
        self.book = SimpleNamespace(abs_id="a1", abs_title="Example Book", ebook_filename="example.epub",
                                    original_ebook_filename="example.epub")
        self.client = Mock()
        self.client.is_configured.return_value = True
        self.client.book_url.side_effect = lambda book_id, url=None: url or f"https://fable.co/book/{book_id}"
        self.client.set_shelf.return_value = True
        self.client.update_progress.return_value = True
        self.client.get_shelf.return_value = (True, None)
        self.client.library.return_value = []
        self.candidate = {"book_id": BOOK_ID, "title": "Example Book", "author": "Example Author"}
        self.client.search_books.return_value = [self.candidate]
        self.client.get_book.return_value = {**self.candidate, "pages": 320, "rating": 4.1, "review_count": 120,
                                             "url": "https://fable.co/book/example", "shelf": None}
        self.abs_client = Mock()
        self.abs_client.get_item_details.return_value = {"media": {"metadata": {
            "title": "Example Book", "authorName": "Example Author", "isbn": "9780000000002"}}}
        self.db = Mock()
        self.db.get_fable_details.return_value = None
        self.db.save_fable_details.side_effect = lambda details: details
        self.sync = FableSyncClient(self.client, Mock(), self.abs_client, self.db)

    def _linked(self):
        self.db.get_fable_details.return_value = FableDetails("a1", BOOK_ID, matched_by="isbn")

    def test_fable_is_a_write_only_tracker(self):
        self.assertFalse(self.sync.can_be_leader())
        self.assertIsNone(self.sync.get_service_state(self.book, None))
        self.assertIn("Fable", _COMPLETION_PROPAGATION_EXCLUDED_CLIENTS)
        self.assertIn("fable", _WRITE_ONLY_CLIENTS)

    def test_automatch_by_isbn_saves_metadata_and_rating(self):
        details = self.sync._automatch(self.book)
        self.client.search_books.assert_called_once_with("9780000000002")
        self.assertEqual((details.fable_book_id, details.matched_by, details.fable_pages, details.fable_rating),
                         (BOOK_ID, "isbn", 320, 4.1))
        self.assertEqual(details.fable_url, "https://fable.co/book/example")

    def test_automatch_prefers_the_copy_on_your_lists(self):
        other = {"book_id": YOURS, "title": "Example Book", "author": "Example Author"}
        self.client.search_books.return_value = [self.candidate, other]
        self.client.get_book.side_effect = lambda book_id: {
            **(other if book_id == YOURS else self.candidate), "shelf": "finished" if book_id == YOURS else None}
        details = self.sync._automatch(self.book)
        self.assertEqual(details.fable_book_id, YOURS)

    def test_automatch_takes_the_book_on_your_lists_first(self):
        # Search shows another edition; yours is on Currently Reading under a different ISBN.
        self.client.library.return_value = [
            {"book_id": YOURS, "title": "Example Book: A Novel", "author": "Example Author", "isbn": "111",
             "shelf": "current_reading"},
            {"book_id": "x", "title": "Another Book", "author": "Example Author", "isbn": "222", "shelf": "finished"},
        ]
        details = self.sync._automatch(self.book)
        self.assertEqual((details.fable_book_id, details.matched_by), (YOURS, "your_list"))
        self.client.search_books.assert_not_called()

    def test_a_sequel_never_matches_the_first_book(self):
        # Fable only had book 1; "Unicorn Breeder 2" fuzzy-matched it at 0.94 in the live probe.
        self.sync._match_metadata = Mock(return_value=("Example Book 2", "Example Author", "", ""))
        self.client.library.return_value = [dict(self.candidate, isbn="1", shelf="finished")]
        self.assertIsNone(self.sync._automatch(self.book))
        self.client.search_books.return_value = [dict(self.candidate, title="Example Book 2: A Novel")]
        self.sync._clear_match_miss("a1")
        self.assertEqual(self.sync._automatch(self.book).fable_book_id, BOOK_ID)

    def test_your_lists_need_the_author_for_a_title_match(self):
        self.abs_client.get_item_details.return_value = {"media": {"metadata": {"title": "Example Book"}}}
        self.client.library.return_value = [
            {"book_id": YOURS, "title": "Example Book", "author": "Someone", "isbn": "111", "shelf": "finished"}]
        with unittest.mock.patch("src.sync_clients.fable_sync_client.resolve_ebook_identifiers", return_value={}):
            details = self.sync._automatch(self.book)
        self.assertNotEqual(getattr(details, "fable_book_id", None), YOURS)

    def test_unreadable_lists_fall_back_to_search(self):
        self.client.library.return_value = None
        details = self.sync._automatch(self.book)
        self.assertEqual(details.fable_book_id, BOOK_ID)

    def test_automatch_miss_waits_before_trying_again(self):
        self.client.search_books.return_value = []
        with self.assertLogs("src.sync_clients.fable_sync_client", "INFO"):
            self.assertIsNone(self.sync._automatch(self.book))
        calls = self.client.search_books.call_count
        self.assertIsNone(self.sync._automatch(self.book))
        self.assertEqual(self.client.search_books.call_count, calls)

    def test_reading_moves_to_current_and_posts_progress(self):
        self._linked()
        result = self.sync.update_progress(self.book, _request(0.42))
        self.assertTrue(result.success)
        self.client.set_shelf.assert_called_once_with(BOOK_ID, "current_reading")
        self.client.update_progress.assert_called_once_with(BOOK_ID, 0.42)

    def test_finishing_moves_to_finished_without_a_progress_post(self):
        self._linked()
        self.client.get_shelf.return_value = (True, "current_reading")
        self.assertTrue(self.sync.update_progress(self.book, _request(0.995)).success)
        self.client.set_shelf.assert_called_once_with(BOOK_ID, "finished")
        self.client.update_progress.assert_not_called()

    def test_a_finished_book_is_never_moved_back(self):
        self._linked()
        for shelf in ("finished", "did_not_finish"):
            self.client.get_shelf.return_value = (True, shelf)
            with self.subTest(shelf=shelf), self.assertLogs("src.sync_clients.fable_sync_client", "INFO"):
                self.assertTrue(self.sync.update_progress(self.book, _request(0.3)).success)
        self.client.set_shelf.assert_not_called()
        self.client.update_progress.assert_not_called()

    def test_want_to_read_only_for_a_book_on_no_list(self):
        self._linked()
        self.assertTrue(self.sync.update_progress(self.book, _request(0.01)).success)
        self.client.set_shelf.assert_called_once_with(BOOK_ID, "want_to_read")
        self.client.set_shelf.reset_mock()
        self.client.get_shelf.return_value = (True, "current_reading")
        self.assertTrue(self.sync.update_progress(self.book, _request(0.01)).success)
        self.client.set_shelf.assert_not_called()
        self.client.update_progress.assert_not_called()

    def test_unreadable_list_or_failed_write_is_a_failure(self):
        self._linked()
        self.client.get_shelf.return_value = (False, None)
        self.assertFalse(self.sync.update_progress(self.book, _request(0.5)).success)
        self.client.get_shelf.return_value = (True, None)
        self.client.set_shelf.return_value = False
        self.assertFalse(self.sync.update_progress(self.book, _request(0.5)).success)
        self.client.update_progress.assert_not_called()

    def test_disabled_fable_does_nothing(self):
        self.client.is_configured.return_value = False
        self.assertFalse(self.sync.update_progress(self.book, _request(0.5)).success)
        self.client.search_books.assert_not_called()


if __name__ == "__main__":
    unittest.main()
