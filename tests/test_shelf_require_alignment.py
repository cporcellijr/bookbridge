import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import src.web_server as web_server
from src.api.booklore_client import BookloreClient
from src.db.models import Book

KEYS = ("BOOKLORE_SHELF_REQUIRE_ALIGNMENT", "BOOKLORE_SHELF_NAME", "BOOKLORE_SHELF_OWNER")


class RequireAlignmentTestCase(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in KEYS}
        os.environ["BOOKLORE_SHELF_REQUIRE_ALIGNMENT"] = "true"
        os.environ["BOOKLORE_SHELF_NAME"] = "ABS Synced"
        os.environ.pop("BOOKLORE_SHELF_OWNER", None)
        self._saved_db = web_server.database_service
        self._saved_globals = web_server._global_clients
        self._saved_container = web_server.container
        self.db = MagicMock()
        web_server.database_service = self.db
        self.client = MagicMock()
        self.client.is_configured.return_value = True
        web_server._global_clients = SimpleNamespace(booklore_client=self.client)

    def tearDown(self):
        web_server.database_service = self._saved_db
        web_server._global_clients = self._saved_globals
        web_server.container = self._saved_container
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _name_owner(self, active=1):
        os.environ["BOOKLORE_SHELF_OWNER"] = "service-account"
        self.db.get_user_by_username.return_value = SimpleNamespace(id=12, active=active)
        owner_client = MagicMock()
        owner_client.is_configured.return_value = True
        container = MagicMock()
        container.user_client_registry.return_value.get_clients.return_value = SimpleNamespace(
            booklore_client=owner_client)
        web_server.container = container
        return owner_client, container


def _book(abs_id, source_id, source="Grimmory"):
    return SimpleNamespace(abs_id=abs_id, abs_title=abs_id, ebook_source=source, ebook_source_id=source_id)


class TestReconcile(RequireAlignmentTestCase):
    def test_off_does_nothing(self):
        os.environ["BOOKLORE_SHELF_REQUIRE_ALIGNMENT"] = "false"
        web_server._reconcile_aligned_shelf()
        self.db.get_books_by_status.assert_not_called()

    def test_adds_only_aligned_grimmory_books_that_are_missing(self):
        self.db.get_books_by_status.return_value = [
            _book("a1", "11"), _book("a2", "22"), _book("a3", "33"), _book("a4", "44", source="ABS"),
        ]
        self.db.has_alignment.side_effect = lambda abs_id: abs_id in ("a1", "a2")
        self.client.list_books_on_shelf.return_value = [{"id": 22}]
        self.client.add_book_id_to_shelf.return_value = True

        added = web_server._reconcile_aligned_shelf()

        self.client.list_books_on_shelf.assert_called_once_with("ABS Synced")
        self.client.add_book_id_to_shelf.assert_called_once_with("11", "ABS Synced")
        self.assertEqual(1, added)

    def test_nothing_aligned_never_touches_grimmory(self):
        self.db.get_books_by_status.return_value = [_book("a1", "11")]
        self.db.has_alignment.return_value = False
        web_server._reconcile_aligned_shelf()
        self.client.list_books_on_shelf.assert_not_called()

    def test_unconfigured_client_is_skipped(self):
        self.client.is_configured.return_value = False
        web_server._reconcile_aligned_shelf()
        self.db.get_books_by_status.assert_not_called()

    def test_errors_are_swallowed(self):
        self.db.get_books_by_status.side_effect = RuntimeError("boom")
        web_server._reconcile_aligned_shelf()

    def test_named_owner_adds_with_its_own_login(self):
        owner_client, container = self._name_owner()
        self.db.get_books_by_status.return_value = [_book("a1", "11")]
        self.db.has_alignment.return_value = True
        owner_client.list_books_on_shelf.return_value = []
        owner_client.add_book_id_to_shelf.return_value = True

        self.assertEqual(1, web_server._reconcile_aligned_shelf())

        self.db.get_user_by_username.assert_called_once_with("service-account")
        container.user_client_registry.return_value.get_clients.assert_called_once_with(12)
        owner_client.add_book_id_to_shelf.assert_called_once_with("11", "ABS Synced")
        self.client.list_books_on_shelf.assert_not_called()

    def test_unknown_or_inactive_owner_never_falls_back_to_the_global_login(self):
        for user in (None, SimpleNamespace(id=12, active=0)):
            with self.subTest(user=user):
                self._name_owner()
                self.db.get_user_by_username.return_value = user
                web_server._reconcile_aligned_shelf()
                self.db.get_books_by_status.assert_not_called()
                self.client.list_books_on_shelf.assert_not_called()


class TestMatchTimeShelving(RequireAlignmentTestCase):
    def test_grimmory_add_is_deferred_when_required(self):
        with patch.object(web_server, "uc", return_value=SimpleNamespace(booklore_client=self.client)), \
                patch.object(web_server, "_is_abs_hosted_ebook_filename", return_value=False):
            web_server._shelve_matched_ebook("book.epub", "grimmory", "11")
        self.client.add_to_shelf.assert_not_called()
        self.client.add_book_id_to_shelf.assert_not_called()

    def test_grimmory_add_still_happens_when_off(self):
        os.environ["BOOKLORE_SHELF_REQUIRE_ALIGNMENT"] = "false"
        self.client.add_to_shelf.return_value = True
        with patch.object(web_server, "uc", return_value=SimpleNamespace(booklore_client=self.client)), \
                patch.object(web_server, "_is_abs_hosted_ebook_filename", return_value=False):
            web_server._shelve_matched_ebook("book.epub", "grimmory", "11")
        self.client.add_to_shelf.assert_called_once_with("book.epub", "ABS Synced")


class TestDeletedMatchLeavesTheShelf(RequireAlignmentTestCase):
    def _cleanup(self, book):
        data_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(data_dir), True)
        if not isinstance(web_server.container, MagicMock):
            web_server.container = MagicMock()
        web_server.container.epub_cache_dir.return_value = str(data_dir)
        self.db.get_all_books.return_value = []
        self.db.delete_kosync_data_for_book.return_value = (0, 0)
        deleter = MagicMock()
        with patch.object(web_server, "uc", return_value=deleter), \
                patch.object(web_server, "DATA_DIR", data_dir, create=True), \
                patch.object(web_server, "manager", None), \
                patch("src.services.shelf_watch_service.clear_shelf_watch_throttle"):
            web_server.cleanup_mapping_resources(book)
        return deleter.booklore_client

    def _book(self, source="Grimmory"):
        return Book(abs_id="a1", abs_title="Title", ebook_filename="title.epub",
                    ebook_source=source, ebook_source_id="45")

    def test_owner_removes_by_id_from_the_shared_shelf(self):
        owner_client, _ = self._name_owner()
        deleter_client = self._cleanup(self._book())
        owner_client.remove_book_id_from_shelf.assert_called_once_with("45", "ABS Synced")
        deleter_client.remove_from_shelf.assert_not_called()

    def test_owner_leaves_books_the_reconcile_never_shelved(self):
        owner_client, _ = self._name_owner()
        deleter_client = self._cleanup(self._book(source="ABS"))
        owner_client.remove_book_id_from_shelf.assert_not_called()
        deleter_client.remove_from_shelf.assert_not_called()

    def test_without_an_owner_the_deleter_removes_from_their_own_shelf(self):
        deleter_client = self._cleanup(self._book())
        deleter_client.remove_from_shelf.assert_called_once_with("title.epub", "ABS Synced")


class TestAddBookIdToShelf(unittest.TestCase):
    def _client(self, shelf_id, response):
        client = BookloreClient.__new__(BookloreClient)
        client._creds = None
        client._get_or_create_shelf_id = MagicMock(return_value=shelf_id)
        client._make_request = MagicMock(return_value=response)
        return client

    def test_assigns_by_id(self):
        client = self._client(18, MagicMock(status_code=200))
        self.assertTrue(client.add_book_id_to_shelf("45", "ABS Synced"))
        client._make_request.assert_called_once_with("POST", "/api/v1/books/shelves", {
            "bookIds": [45], "shelvesToAssign": [18], "shelvesToUnassign": []})

    def test_missing_shelf_or_failure_is_false(self):
        self.assertFalse(self._client(None, MagicMock(status_code=200)).add_book_id_to_shelf("45", "ABS Synced"))
        self.assertFalse(self._client(18, MagicMock(status_code=401)).add_book_id_to_shelf("45", "ABS Synced"))
        self.assertFalse(self._client(18, MagicMock(status_code=200)).add_book_id_to_shelf("", "ABS Synced"))


class TestRemoveBookIdFromShelf(unittest.TestCase):
    def _client(self, shelf_id, response):
        client = BookloreClient.__new__(BookloreClient)
        client._creds = None
        client._get_shelf_id = MagicMock(return_value=shelf_id)
        client._make_request = MagicMock(return_value=response)
        return client

    def test_unassigns_by_id(self):
        client = self._client(18, MagicMock(status_code=200))
        self.assertTrue(client.remove_book_id_from_shelf("45", "ABS Synced"))
        client._make_request.assert_called_once_with("POST", "/api/v1/books/shelves", {
            "bookIds": [45], "shelvesToAssign": [], "shelvesToUnassign": [18]})

    def test_missing_shelf_or_failure_is_false(self):
        missing = self._client(None, MagicMock(status_code=200))
        self.assertFalse(missing.remove_book_id_from_shelf("45", "ABS Synced"))
        missing._make_request.assert_not_called()
        self.assertFalse(self._client(18, MagicMock(status_code=403)).remove_book_id_from_shelf("45", "ABS Synced"))
        self.assertFalse(self._client(18, MagicMock(status_code=200)).remove_book_id_from_shelf("", "ABS Synced"))


if __name__ == "__main__":
    unittest.main()
