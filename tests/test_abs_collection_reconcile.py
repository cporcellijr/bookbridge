"""Matched audiobooks are kept in their owner's ABS auto-add collection.

The collection used to be filled only by the one add made when a book is
matched, so an add that failed then (ABS briefly unreachable, a busy daemon)
left the book out for good. The reconcile re-adds whatever is missing each
sync cycle.
"""

import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import src.web_server as web_server
from src.api.api_clients import ABSClient


def _book(abs_id, user_id=1, audio_source="ABS", sync_mode="audiobook"):
    return SimpleNamespace(abs_id=abs_id, abs_title=abs_id, audio_source=audio_source,
                           sync_mode=sync_mode, user_id=user_id)


class ReconcileTestCase(unittest.TestCase):
    def setUp(self):
        self._saved = (web_server.database_service, web_server.container)
        self.db = Mock()
        self.db._default_user_id.return_value = 1
        self.db.get_user_credentials.return_value = {"ABS_COLLECTION_NAME": "Ebook Synced"}
        self.clients = {}
        registry = Mock()
        registry.get_clients.side_effect = lambda uid: SimpleNamespace(abs_client=self.client(uid))
        container = Mock()
        container.user_client_registry.return_value = registry
        web_server.database_service = self.db
        web_server.container = container
        self.env = patch.dict(os.environ, {"ABS_COLLECTION_RECONCILE": "true"})
        self.env.start()
        os.environ.pop("ABS_COLLECTION_NAME", None)

    def tearDown(self):
        self.env.stop()
        web_server.database_service, web_server.container = self._saved

    def client(self, uid, present=()):
        if uid not in self.clients:
            c = Mock()
            c.is_configured.return_value = True
            c.list_collection_item_ids.return_value = set(present)
            c.add_to_collection.return_value = True
            self.clients[uid] = c
        return self.clients[uid]

    def books(self, *books):
        self.db.get_books_by_status.return_value = list(books)


class TestAbsCollectionReconcile(ReconcileTestCase):
    def test_off_by_default_touches_nothing(self):
        os.environ.pop("ABS_COLLECTION_RECONCILE", None)
        self.books(_book("a"))

        self.assertIsNone(web_server._reconcile_abs_collection())
        self.db.get_books_by_status.assert_not_called()

    def test_adds_only_the_missing_audiobooks(self):
        self.client(1, present={"a"})
        self.books(_book("a"), _book("b"))

        self.assertEqual(web_server._reconcile_abs_collection(), 1)
        self.clients[1].add_to_collection.assert_called_once_with("b", "Ebook Synced")

    def test_books_without_abs_audio_are_left_alone(self):
        self.books(
            _book("booklore:12", audio_source="BookLore"),
            _book("bookorbit:7", audio_source="BookOrbit"),
            _book("ebook-0123456789abcdef", audio_source=None, sync_mode="ebook_only"),
            _book("booklore:99", audio_source=None),
        )

        self.assertEqual(web_server._reconcile_abs_collection(), 0)
        self.assertEqual(self.clients, {})

    def test_legacy_rows_without_an_audio_source_count_as_abs(self):
        self.books(_book("legacy", audio_source=None))

        self.assertEqual(web_server._reconcile_abs_collection(), 1)
        self.clients[1].add_to_collection.assert_called_once_with("legacy", "Ebook Synced")

    def test_each_owner_uses_its_own_client_and_collection(self):
        self.db.get_user_credentials.side_effect = lambda uid: {
            1: {"ABS_COLLECTION_NAME": "Ebook Synced"},
            12: {"ABS_COLLECTION_NAME": "Shared Picks"},
        }[uid]
        self.books(_book("a", user_id=1), _book("b", user_id=12))

        self.assertEqual(web_server._reconcile_abs_collection(), 2)
        self.clients[1].add_to_collection.assert_called_once_with("a", "Ebook Synced")
        self.clients[12].add_to_collection.assert_called_once_with("b", "Shared Picks")
        self.clients[12].list_collection_item_ids.assert_called_once_with("Shared Picks")

    def test_unowned_books_go_through_the_primary_admin(self):
        self.books(_book("a", user_id=None))

        self.assertEqual(web_server._reconcile_abs_collection(), 1)
        self.clients[1].add_to_collection.assert_called_once_with("a", "Ebook Synced")

    def test_collection_name_falls_back_to_the_global_setting_then_the_default(self):
        self.db.get_user_credentials.return_value = {}
        self.books(_book("a"))
        with patch.dict(os.environ, {"ABS_COLLECTION_NAME": "Global Shelf"}):
            web_server._reconcile_abs_collection()
        self.clients[1].add_to_collection.assert_called_with("a", "Global Shelf")

        self.clients.clear()
        web_server._reconcile_abs_collection()
        self.clients[1].add_to_collection.assert_called_with("a", "Synced with KOReader")

    def test_an_unconfigured_owner_is_skipped(self):
        self.client(1).is_configured.return_value = False
        self.books(_book("a"))

        self.assertEqual(web_server._reconcile_abs_collection(), 0)
        self.clients[1].list_collection_item_ids.assert_not_called()
        self.clients[1].add_to_collection.assert_not_called()

    def test_an_unreadable_collection_adds_nothing(self):
        self.client(1).list_collection_item_ids.return_value = None
        self.books(_book("a"), _book("b"))

        self.assertEqual(web_server._reconcile_abs_collection(), 0)
        self.clients[1].add_to_collection.assert_not_called()

    def test_a_failed_add_is_not_counted_and_the_rest_continue(self):
        self.client(1).add_to_collection.side_effect = lambda abs_id, name: abs_id != "a"
        self.books(_book("a"), _book("b"))

        self.assertEqual(web_server._reconcile_abs_collection(), 1)
        self.assertEqual(self.clients[1].add_to_collection.call_count, 2)

    def test_errors_are_contained(self):
        self.db.get_books_by_status.side_effect = RuntimeError("db gone")

        self.assertIsNone(web_server._reconcile_abs_collection())


class TestListCollectionItemIds(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"ABS_SERVER": "http://abs.example", "ABS_KEY": "token"})
        self.env.start()
        self.client = ABSClient()
        self.client.session = MagicMock()

    def tearDown(self):
        self.env.stop()

    def respond(self, status, payload):
        resp = MagicMock()
        resp.status_code = status
        resp.json.return_value = payload
        self.client.session.get.return_value = resp

    def test_returns_the_ids_in_the_named_collection(self):
        self.respond(200, {"collections": [
            {"name": "Other", "books": [{"id": "x"}]},
            {"name": "Ebook Synced", "books": [{"id": "a"}, {"id": "b"}, {}]},
        ]})

        self.assertEqual(self.client.list_collection_item_ids("Ebook Synced"), {"a", "b"})

    def test_a_missing_collection_is_empty(self):
        self.respond(200, {"collections": [{"name": "Other", "books": [{"id": "x"}]}]})

        self.assertEqual(self.client.list_collection_item_ids("Ebook Synced"), set())

    def test_a_failed_listing_is_none(self):
        self.respond(500, {})

        self.assertIsNone(self.client.list_collection_item_ids("Ebook Synced"))


class TestAddToCollectionReportsFailures(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"ABS_SERVER": "http://abs.example", "ABS_KEY": "token"})
        self.env.start()
        self.client = ABSClient()
        self.client.session = MagicMock()

    def tearDown(self):
        self.env.stop()

    def test_a_rejected_add_is_logged(self):
        listing = MagicMock(status_code=200)
        listing.json.return_value = {"collections": [{"name": "Ebook Synced", "id": "col-1"}]}
        self.client.session.get.return_value = listing
        self.client.session.post.return_value = MagicMock(status_code=403, text="forbidden")

        with self.assertLogs("src.api.api_clients", level="WARNING") as logs:
            self.assertFalse(self.client.add_to_collection("item-1", "Ebook Synced"))
        self.assertIn("add returned 403", "\n".join(logs.output))

    def test_a_failed_listing_is_logged(self):
        self.client.session.get.return_value = MagicMock(status_code=502)

        with self.assertLogs("src.api.api_clients", level="WARNING") as logs:
            self.assertFalse(self.client.add_to_collection("item-1", "Ebook Synced"))
        self.assertIn("listing collections returned 502", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
