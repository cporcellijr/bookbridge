"""Regression tests for `ABSClient.add_to_collection` (diagnostics finding 2808).

Two defects behind one misleading warning, still firing on 7.5.0 after the
dev-963 crash guard:

    Failed to add item to ABS collection 't:26e7854c': collection id unavailable

  * the auto-create branch picked `libraries[0]` regardless of which library
    actually holds the item, and an ABS collection only accepts books from its
    own library;
  * every way the create could fail fell through to the warning above, which
    named a cause ('collection id unavailable') that was never the real one.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.api.api_clients import ABSClient


class TestABSCollectionCreation(unittest.TestCase):
    def setUp(self):
        self.env_patch = patch.dict(
            os.environ,
            {"ABS_SERVER": "http://abs.example", "ABS_KEY": "token"},
            clear=False,
        )
        self.env_patch.start()
        self.client = ABSClient()
        self.client.session = MagicMock()
        self.libraries = [
            {"id": "lib-audiobooks"},
            {"id": "lib-ebooks"},
        ]
        self.item = {"id": "item-1", "libraryId": "lib-ebooks"}
        self.collections = []
        self.client.session.get.side_effect = self._get
        self.create_response = self._response(200, {})
        self.client.session.post.return_value = self.create_response

    def tearDown(self):
        self.env_patch.stop()

    @staticmethod
    def _response(status, payload, text=""):
        resp = MagicMock()
        resp.status_code = status
        resp.json.return_value = payload
        resp.text = text
        return resp

    def _get(self, url, **kwargs):
        """Return the configured collections, item, and libraries."""
        if url.endswith("/api/collections"):
            return self._response(200, {"collections": self.collections})
        if "/api/items/" in url:
            if self.item is None:
                return self._response(404, {})
            return self._response(200, self.item)
        if url.endswith("/api/libraries"):
            return self._response(200, {"libraries": self.libraries})
        raise AssertionError(f"unexpected GET {url}")

    def _created_payload(self):
        self.client.session.post.assert_called_once()
        _args, kwargs = self.client.session.post.call_args
        return kwargs["json"]

    def test_creates_the_collection_in_the_items_own_library(self):
        self.assertTrue(self.client.add_to_collection("item-1", "Synced with KOReader"))

        self.assertEqual(self._created_payload()["libraryId"], "lib-ebooks")

    def test_falls_back_to_the_only_library_when_the_item_lookup_fails(self):
        self.item = None
        self.libraries = [{"id": "lib-only"}]

        self.assertTrue(self.client.add_to_collection("item-1", "Synced with KOReader"))

        self.assertEqual(self._created_payload()["libraryId"], "lib-only")

    def test_ambiguous_library_is_not_guessed(self):
        """Two libraries and no item to resolve: guessing is what caused #2808."""
        self.item = None

        with self.assertLogs("src.api.api_clients", level="WARNING") as logs:
            self.assertFalse(self.client.add_to_collection("item-1", "Synced with KOReader"))

        self.client.session.post.assert_not_called()
        self.assertNotIn("collection id unavailable", " ".join(logs.output))

    def test_create_failure_reports_what_abs_answered(self):
        self.client.session.post.return_value = self._response(500, {}, text="Internal Server Error")

        with self.assertLogs("src.api.api_clients", level="WARNING") as logs:
            self.assertFalse(self.client.add_to_collection("item-1", "Synced with KOReader"))

        message = " ".join(logs.output)
        self.assertIn("500", message)
        self.assertIn("Internal Server Error", message)
        self.assertNotIn("collection id unavailable", message)

    def test_blank_explicit_collection_name_skips_requests_instead_of_using_global(self):
        with patch.dict(os.environ, {"ABS_COLLECTION_NAME": "Global Shelf"}):
            for name in ("", " \t "):
                with self.subTest(name=repr(name)):
                    self.client.session.get.reset_mock()
                    self.client.session.post.reset_mock()

                    self.assertFalse(self.client.add_to_collection("item-1", name))

                    self.client.session.get.assert_not_called()
                    self.client.session.post.assert_not_called()

    def test_blank_configured_collection_name_skips_requests(self):
        with patch.dict(os.environ, {"ABS_COLLECTION_NAME": "  \t "}):
            self.assertFalse(self.client.add_to_collection("item-1"))

        self.client.session.get.assert_not_called()
        self.client.session.post.assert_not_called()

    def test_unset_collection_name_uses_configured_default(self):
        with patch.dict(os.environ, {"ABS_COLLECTION_NAME": "Configured Shelf"}):
            self.assertTrue(self.client.add_to_collection("item-1"))

        self.assertEqual(self._created_payload()["name"], "Configured Shelf")

    def test_blank_missing_collection_name_skips_all_requests(self):
        self.assertEqual(self.client.add_missing_to_collection(["item-1"], " \t "), (0, []))

        self.client.session.get.assert_not_called()
        self.client.session.post.assert_not_called()

    def test_add_to_existing_collection_treats_duplicate400_as_success(self):
        self.collections = [{"id": "col-1", "name": "Synced with KOReader", "libraryId": "lib-ebooks"}]
        self.client.session.post.return_value = self._response(400, {}, text="Book already in collection")
        with self.assertNoLogs("src.api.api_clients", level="WARNING"):
            result = self.client.add_to_collection("item-1", "Synced with KOReader")
        self.assertTrue(result)

    def test_add_to_existing_collection_fails_on_other_400(self):
        self.collections = [{"id": "col-1", "name": "Synced with KOReader", "libraryId": "lib-ebooks"}]
        self.client.session.post.return_value = self._response(400, {}, text="Invalid request")
        with self.assertLogs("src.api.api_clients", level="WARNING") as logs:
            result = self.client.add_to_collection("item-1", "Synced with KOReader")
        self.assertFalse(result)
        self.assertTrue(any("add returned 400" in msg for msg in logs.output))

    def test_add_to_existing_collection_fails_on_partial_match_body(self):
        self.collections = [{"id": "col-1", "name": "Synced with KOReader", "libraryId": "lib-ebooks"}]
        self.client.session.post.return_value = self._response(400, {}, text="Book already in collection: item-1")
        with self.assertLogs("src.api.api_clients", level="WARNING") as logs:
            result = self.client.add_to_collection("item-1", "Synced with KOReader")
        self.assertFalse(result)
        self.assertTrue(any("add returned 400" in msg for msg in logs.output))

    def test_add_to_existing_collection_fails_on_500_even_with_duplicate_text(self):
        self.collections = [{"id": "col-1", "name": "Synced with KOReader", "libraryId": "lib-ebooks"}]
        self.client.session.post.return_value = self._response(500, {}, text="Book already in collection")
        with self.assertLogs("src.api.api_clients", level="WARNING") as logs:
            result = self.client.add_to_collection("item-1", "Synced with KOReader")
        self.assertFalse(result)
        self.assertTrue(any("add returned 500" in msg for msg in logs.output))

    def _same_name_collections(self):
        """Diagnostics finding 9154: one collection name in two libraries."""
        return [
            {"id": "col-audio", "name": "Synced with KOReader", "libraryId": "lib-audiobooks",
             "books": [{"id": "item-1"}]},
            {"id": "col-ebooks", "name": "Synced with KOReader", "libraryId": "lib-ebooks", "books": []},
        ]

    def test_add_uses_the_same_name_collection_in_the_items_library(self):
        self.collections = self._same_name_collections()

        self.assertTrue(self.client.add_to_collection("item-1", "Synced with KOReader"))

        args, _kwargs = self.client.session.post.call_args
        self.assertEqual(args[0], "http://abs.example/api/collections/col-ebooks/book")

    def test_add_creates_a_collection_when_only_another_library_has_the_name(self):
        self.collections = self._same_name_collections()[:1]

        self.assertTrue(self.client.add_to_collection("item-1", "Synced with KOReader"))

        payload = self._created_payload()
        self.assertEqual(payload["libraryId"], "lib-ebooks")
        self.assertEqual(payload["books"], ["item-1"])

    def test_remove_uses_the_same_name_collection_that_holds_the_item(self):
        self.collections = list(reversed(self._same_name_collections()))
        self.client.session.delete.return_value = self._response(200, {})
        with patch.object(self.client, "is_configured", return_value=True):
            self.assertTrue(self.client.remove_from_collection("item-1", "Synced with KOReader"))

        args, _kwargs = self.client.session.delete.call_args
        self.assertEqual(args[0], "http://abs.example/api/collections/col-audio/book/item-1")


if __name__ == "__main__":
    unittest.main()
