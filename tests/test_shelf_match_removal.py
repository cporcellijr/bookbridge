"""Tests for `_shelve_matched_ebook`: approving/matching a book should add it to
the Kobo shelf and clear it from the shelf-watch "Up Next" shelf, mirroring the
auto-match move.
"""

import os
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

import src.web_server as web_server


class TestShelveMatchedEbook(unittest.TestCase):
    def setUp(self):
        self.booklore = Mock()
        self.booklore.is_configured.return_value = True
        container = Mock()
        container.booklore_client.return_value = self.booklore

        self._container_patch = patch.object(web_server, "container", container)
        self._shelf_patch = patch.object(web_server, "BOOKLORE_SHELF_NAME", "Kobo")
        self._container_patch.start()
        self._shelf_patch.start()

    def tearDown(self):
        self._container_patch.stop()
        self._shelf_patch.stop()

    def test_removes_from_watch_shelf_when_enabled(self):
        with patch.dict(os.environ, {
            "BOOKLORE_SHELF_WATCH_ENABLED": "true",
            "BOOKLORE_SHELF_WATCH_NAME": "Up Next",
        }):
            web_server._shelve_matched_ebook("book.epub")

        self.booklore.add_to_shelf.assert_called_once_with("book.epub", "Kobo")
        self.booklore.remove_from_shelf.assert_called_once_with("book.epub", "Up Next")

    def test_does_not_remove_when_watch_disabled(self):
        with patch.dict(os.environ, {
            "BOOKLORE_SHELF_WATCH_ENABLED": "false",
            "BOOKLORE_SHELF_WATCH_NAME": "Up Next",
        }):
            web_server._shelve_matched_ebook("book.epub")

        self.booklore.add_to_shelf.assert_called_once_with("book.epub", "Kobo")
        self.booklore.remove_from_shelf.assert_not_called()

    def test_does_not_remove_when_watch_shelf_equals_kobo(self):
        with patch.dict(os.environ, {
            "BOOKLORE_SHELF_WATCH_ENABLED": "true",
            "BOOKLORE_SHELF_WATCH_NAME": "Kobo",
        }):
            web_server._shelve_matched_ebook("book.epub")

        self.booklore.add_to_shelf.assert_called_once_with("book.epub", "Kobo")
        self.booklore.remove_from_shelf.assert_not_called()

    def test_noop_when_booklore_not_configured(self):
        self.booklore.is_configured.return_value = False
        with patch.dict(os.environ, {"BOOKLORE_SHELF_WATCH_ENABLED": "true"}):
            web_server._shelve_matched_ebook("book.epub")

        self.booklore.add_to_shelf.assert_not_called()
        self.booklore.remove_from_shelf.assert_not_called()

    def test_noop_when_no_filename(self):
        with patch.dict(os.environ, {"BOOKLORE_SHELF_WATCH_ENABLED": "true"}):
            web_server._shelve_matched_ebook("")

        self.booklore.add_to_shelf.assert_not_called()
        self.booklore.remove_from_shelf.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
