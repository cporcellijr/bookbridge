"""A series volume must never title-match its neighbour on any tracker.

Live on Fable (2026-11-27), "Unicorn Breeder 2" fuzzy-matched the tracker's only
"Unicorn Breeder" (book 1); the same title scoring drives Goodreads, StoryGraph and
Hardcover automatching. An ISBN hit stays trusted whatever its title says.
"""

import os
import unittest
from unittest.mock import Mock, patch

from src.api.fable_client import FableClient
from src.api.goodreads_client import GoodreadsClient
from src.api.hardcover_client import HardcoverClient
from src.api.storygraph_client import StorygraphClient
from src.sync_clients.goodreads_sync_client import GoodreadsSyncClient
from src.utils.string_utils import same_volume

AUTHOR = "Virgil Knightley"
BOOK_ONE = "Unicorn Breeder"
BOOK_TWO = "Unicorn Breeder 2"


class TestSameVolume(unittest.TestCase):
    def test_volume_numbers(self):
        self.assertFalse(same_volume(BOOK_TWO, BOOK_ONE))
        self.assertFalse(same_volume(BOOK_ONE, BOOK_TWO))
        self.assertTrue(same_volume(BOOK_TWO, "Unicorn Breeder 2: A LitRPG Adventure"))
        self.assertTrue(same_volume("Exile", "Exile: De'Vas Chronicles Book 1"))
        self.assertTrue(same_volume("Dune", "Dune (Dune, #1)"))
        self.assertTrue(same_volume("", ""))


class TestHardcoverVolumeGuard(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict(os.environ, {"HARDCOVER_TOKEN": "t", "HARDCOVER_ENABLED": "true"}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = HardcoverClient()
        self.client.get_default_edition = Mock(return_value={"id": 9, "pages": 300})

    def _candidate(self, book_id, title):
        return {"id": book_id, "title": title, "slug": f"s{book_id}",
                "cached_contributors": [{"name": AUTHOR}]}

    def test_sequel_does_not_match_book_one(self):
        self.client._search_candidate_books = Mock(return_value=[self._candidate(1, BOOK_ONE)])
        self.assertIsNone(self.client.search_by_title_author(BOOK_TWO, AUTHOR))

    def test_sequel_matches_its_own_volume(self):
        self.client._search_candidate_books = Mock(return_value=[
            self._candidate(1, BOOK_ONE), self._candidate(2, BOOK_TWO)])
        self.assertEqual(self.client.search_by_title_author(BOOK_TWO, AUTHOR)["book_id"], 2)


class TestStorygraphVolumeGuard(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict(os.environ, {"STORYGRAPH_ENABLED": "true", "STORYGRAPH_SESSION_COOKIE": "s",
                                          "STORYGRAPH_REMEMBER_USER_TOKEN": "r"}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = StorygraphClient()
        self.client.book_url = Mock(side_effect=lambda b: f"http://sg/books/{b}")

    def test_title_hit_for_book_one_is_rejected(self):
        self.client.search_books = Mock(return_value=[{"book_id": "b1", "title": BOOK_ONE, "author": AUTHOR}])
        self.assertIsNone(self.client.resolve_book(title=BOOK_TWO, author=AUTHOR))

    def test_isbn_hit_is_trusted(self):
        self.client.search_books = Mock(side_effect=lambda query, author: (
            [{"book_id": "b2", "title": "Unicorn Breeder Two", "author": AUTHOR}] if query == "9780000000002"
            else [{"book_id": "b1", "title": BOOK_ONE, "author": AUTHOR}]))
        self.assertEqual(self.client.resolve_book(title=BOOK_TWO, author=AUTHOR, isbn="9780000000002")["book_id"], "b2")


class TestSearchResolveVolumeGuard(unittest.TestCase):
    """Goodreads and Fable clients share the same resolve_book shape."""

    def _check(self, client):
        client.search_books = Mock(return_value=[{"book_id": "b1", "title": BOOK_ONE, "author": AUTHOR}])
        self.assertIsNone(client.resolve_book(BOOK_TWO, AUTHOR))
        client.search_books = Mock(side_effect=lambda query: (
            [{"book_id": "b2", "title": "Unicorn Breeder Two", "author": AUTHOR}] if query == "9780000000002" else []))
        self.assertEqual(client.resolve_book(BOOK_TWO, AUTHOR, "9780000000002")["book_id"], "b2")

    def test_goodreads(self):
        self._check(GoodreadsClient({}))

    def test_fable(self):
        self._check(FableClient({}))


class TestGoodreadsAutomatchVolumeGuard(unittest.TestCase):
    def test_title_pick_rejects_book_one_but_isbn_pick_is_trusted(self):
        book_one = [{"book_id": "1", "title": BOOK_ONE, "author": AUTHOR}]
        self.assertIsNone(GoodreadsSyncClient._best(book_one, BOOK_TWO, AUTHOR, 0.65))
        self.assertEqual(GoodreadsSyncClient._best(book_one, BOOK_TWO, AUTHOR)["book_id"], "1")


if __name__ == "__main__":
    unittest.main()
