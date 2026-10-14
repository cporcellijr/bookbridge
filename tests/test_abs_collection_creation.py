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
        """Dispatch on URL: no collection exists yet, so the create branch runs."""
        if url.endswith("/api/collections"):
            return self._response(200, {"collections": []})
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


if __name__ == "__main__":
    unittest.main()
