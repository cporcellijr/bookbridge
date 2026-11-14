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
        self.booklore.add_to_shelf.return_value = True
        self.bookorbit = Mock()
        self.bookorbit.is_configured.return_value = True
        self.bookorbit.add_book_id_to_shelf.return_value = True
        self.bookorbit._shelf_key.side_effect = (
            lambda name: (name or "").strip().lower()
        )
        self.kavita = Mock()
        self.kavita.is_configured.return_value = True
        self.kavita.add_book_id_to_shelf.return_value = True
        self.kavita._shelf_key.side_effect = (
            lambda name: (name or "").strip().casefold()
        )
        container = Mock()
        container.booklore_client.return_value = self.booklore
        container.bookorbit_client.return_value = self.bookorbit
        container.kavita_client.return_value = self.kavita

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

    def test_abs_hosted_ebook_is_not_sent_to_grimmory(self):
        """Diagnostics finding 4184/4187 - the fallback branch was a catch-all.

        An ABS-hosted ebook has no Grimmory catalog entry, so every cycle logged:
            ⚠️ Grimmory: Book not found for shelf assignment: 39fed857-875e-41f0-b41c-04ba9e361fa4_abs.epub
            ⚠️ Grimmory: Book not found for shelf removal: 39fed857-875e-41f0-b41c-04ba9e361fa4_abs.epub
        """
        with patch.dict(os.environ, {
            "BOOKLORE_SHELF_WATCH_ENABLED": "true",
            "BOOKLORE_SHELF_WATCH_NAME": "Up Next",
        }):
            web_server._shelve_matched_ebook(
                "39fed857-875e-41f0-b41c-04ba9e361fa4_abs.epub", "ABS",
                "39fed857-875e-41f0-b41c-04ba9e361fa4",
            )

        self.booklore.add_to_shelf.assert_not_called()
        self.booklore.remove_from_shelf.assert_not_called()

    def test_abs_source_is_skipped_even_without_the_filename_marker(self):
        """Source is authoritative: a Grimmory-shaped name on an ABS book still skips."""
        with patch.dict(os.environ, {
            "BOOKLORE_SHELF_WATCH_ENABLED": "true",
            "BOOKLORE_SHELF_WATCH_NAME": "Up Next",
        }):
            web_server._shelve_matched_ebook("Some Book - Author.epub", "ABS")

        self.booklore.add_to_shelf.assert_not_called()

    def test_unrecorded_source_still_shelves_on_grimmory(self):
        """Legacy rows and /books-mount installs never recorded an ebook_source."""
        with patch.dict(os.environ, {
            "BOOKLORE_SHELF_WATCH_ENABLED": "false",
        }):
            web_server._shelve_matched_ebook("book.epub", None)

        self.booklore.add_to_shelf.assert_called_once_with("book.epub", "Kobo")

    def test_explicit_booklore_source_still_shelves_on_grimmory(self):
        with patch.dict(os.environ, {
            "BOOKLORE_SHELF_WATCH_ENABLED": "false",
        }):
            web_server._shelve_matched_ebook("book.epub", "BookLore", 42)

        self.booklore.add_to_shelf.assert_called_once_with("book.epub", "Kobo")

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

    def test_bookorbit_does_not_remove_when_shelf_names_differ_only_by_case(self):
        with patch.object(web_server, "user_setting", return_value="Kobo"), patch.dict(
            os.environ,
            {
                "BOOKORBIT_SHELF_WATCH_ENABLED": "true",
                "BOOKORBIT_SHELF_WATCH_NAME": "kobo",
            },
        ):
            web_server._shelve_matched_ebook("book.epub", "BookOrbit", 17)

        self.bookorbit.add_book_id_to_shelf.assert_called_once_with(17, "Kobo")
        self.bookorbit.remove_book_id_from_shelf.assert_not_called()

    def test_kavita_moves_matched_chapter_out_of_watch_collection(self):
        with patch.object(
            web_server,
            "user_setting",
            side_effect=lambda key, default=None: (
                "BookBridge" if key == "KAVITA_COLLECTION_NAME" else default
            ),
        ), patch.dict(
            os.environ,
            {
                "KAVITA_SHELF_WATCH_ENABLED": "true",
                "KAVITA_SHELF_WATCH_NAME": "Up Next",
            },
        ):
            web_server._shelve_matched_ebook("book.epub", "Kavita", 23)

        self.kavita.add_book_id_to_shelf.assert_called_once_with(23, "BookBridge")
        self.kavita.remove_book_id_from_shelf.assert_called_once_with(23, "Up Next")

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

    def test_does_not_remove_watch_shelf_when_destination_add_fails(self):
        self.booklore.add_to_shelf.return_value = False
        with patch.dict(os.environ, {
            "BOOKLORE_SHELF_WATCH_ENABLED": "true",
            "BOOKLORE_SHELF_WATCH_NAME": "Up Next",
        }):
            web_server._shelve_matched_ebook("book.epub")

        self.booklore.add_to_shelf.assert_called_once_with("book.epub", "Kobo")
        self.booklore.remove_from_shelf.assert_not_called()

    def test_does_not_remove_watch_shelf_when_destination_add_raises(self):
        self.booklore.add_to_shelf.side_effect = RuntimeError("destination unavailable")
        with patch.dict(os.environ, {
            "BOOKLORE_SHELF_WATCH_ENABLED": "true",
            "BOOKLORE_SHELF_WATCH_NAME": "Up Next",
        }):
            web_server._shelve_matched_ebook("book.epub")

        self.booklore.remove_from_shelf.assert_not_called()

    def test_remove_only_same_shelf_is_a_successful_noop(self):
        metadata = {"grimmory_filename": "book.epub"}
        with patch(
            "src.web.shelf_watch_service._shelf_watch_clients_for", return_value=(self.booklore, "Kobo", "Kobo")
        ):
            self.assertTrue(web_server._complete_shelf_watch_approval(metadata, remove_only=True))

        self.booklore.remove_from_shelf.assert_not_called()
        self.booklore.move_between_shelves.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
