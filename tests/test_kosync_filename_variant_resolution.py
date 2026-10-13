"""Unit tests for the filename-variant fallback in _resolve_book_by_sibling_hash.

KOReader's filename matching hashes the device copy's basename. Calibre names the
library file "Title - Author.epub" but sends the device copy as "Author - Title.epub",
so the device hash never equals any hash the bridge computes from the library name.
"""

import hashlib
import unicodedata
import unittest
from unittest.mock import MagicMock, patch

from src.api import kosync_server


def _md5(name: str, form: str = "NFC") -> str:
    return hashlib.md5(unicodedata.normalize(form, name).encode("utf-8")).hexdigest()


class TestFilenameVariantResolution(unittest.TestCase):
    def setUp(self):
        self._saved_db = kosync_server._database_service
        self.addCleanup(self._restore)
        self.db = MagicMock()
        self.db.get_kosync_document.return_value = None
        self.books = {}
        self.db.get_books_by_status.side_effect = lambda status, user_id=None: list(self.books.values())
        self.db.get_book.side_effect = lambda abs_id: self.books.get(abs_id)
        kosync_server._database_service = self.db
        kosync_server._invalidate_filename_variant_index()

    def _restore(self):
        kosync_server._database_service = self._saved_db
        kosync_server._invalidate_filename_variant_index()

    def _book(self, abs_id, original=None, ebook=None):
        book = MagicMock()
        book.abs_id = abs_id
        book.abs_title = abs_id
        book.original_ebook_filename = original
        book.ebook_filename = ebook
        self.books[abs_id] = book
        return book

    def _resolve(self, doc_id, **kwargs):
        kwargs.setdefault("filename_variants", True)
        kwargs.setdefault("user_id", 7)
        kosync_server._invalidate_filename_variant_index()
        return kosync_server._resolve_book_by_sibling_hash(doc_id, **kwargs)

    # ---- the reported case ----

    def test_calibre_author_first_device_name_resolves_to_title_first_library_file(self):
        book = self._book(
            "bolano",
            original="By Night in Chile - Roberto Bolaño.epub",
            ebook="storyteller_affdc710-2a67-4718-b177-bbea772e5148.epub",
        )
        device_hash = "5e27029aefa667a923c4c57f9d33fc48"
        self.assertEqual(device_hash, _md5("Roberto Bolaño - By Night in Chile.epub"))

        self.assertIs(self._resolve(device_hash), book)
        self.db.get_books_by_status.assert_called_with("active", user_id=7)

    def test_variants_are_off_unless_the_caller_opts_in(self):
        self._book("bolano", original="By Night in Chile - Roberto Bolaño.epub")

        result = kosync_server._resolve_book_by_sibling_hash("5e27029aefa667a923c4c57f9d33fc48")

        self.assertIsNone(result)
        self.db.get_books_by_status.assert_not_called()

    def test_scope_follows_the_requesting_user(self):
        self._book("bolano", original="By Night in Chile - Roberto Bolaño.epub")

        self._resolve("5e27029aefa667a923c4c57f9d33fc48", user_id=None)

        self.db.get_books_by_status.assert_called_with("active", user_id=None)

    # ---- name shapes ----

    def test_author_first_library_file_matches_title_first_device_name(self):
        book = self._book("b", original="Roberto Bolaño - By Night in Chile.epub")
        self.assertIs(self._resolve(_md5("By Night in Chile - Roberto Bolaño.epub")), book)

    def test_three_part_names_rotate_in_both_directions(self):
        title_first = self._book("t", original="Star Wars - Thrawn - Timothy Zahn.epub")
        self.assertIs(self._resolve(_md5("Timothy Zahn - Star Wars - Thrawn.epub")), title_first)

        self.books.clear()
        author_first = self._book("a", original="Timothy Zahn - Star Wars - Thrawn.epub")
        self.assertIs(self._resolve(_md5("Star Wars - Thrawn - Timothy Zahn.epub")), author_first)

    def test_dotted_names_keep_their_dots(self):
        hobbit = self._book("hobbit", original="The Hobbit - J.R.R. Tolkien.epub")
        self.assertIs(self._resolve(_md5("J.R.R. Tolkien - The Hobbit.epub")), hobbit)

        self.books.clear()
        mercedes = self._book("mercedes", original="Mr. Mercedes - Stephen King.epub")
        self.assertIs(self._resolve(_md5("Stephen King - Mr. Mercedes.epub")), mercedes)

    def test_compound_and_uppercase_extensions_are_preserved(self):
        kepub = self._book("kepub", original="Dune - Frank Herbert.kepub.epub")
        self.assertIs(self._resolve(_md5("Frank Herbert - Dune.kepub.epub")), kepub)

        self.books.clear()
        upper = self._book("upper", original="Dune - Frank Herbert.EPUB")
        self.assertIs(self._resolve(_md5("Frank Herbert - Dune.EPUB")), upper)

    def test_unicode_normalization_differences_still_match(self):
        nfd_library = self._book("nfd", original=unicodedata.normalize("NFD", "By Night in Chile - Roberto Bolaño.epub"))
        self.assertIs(self._resolve(_md5("Roberto Bolaño - By Night in Chile.epub", "NFC")), nfd_library)

        self.books.clear()
        nfc_library = self._book("nfc", original="By Night in Chile - Roberto Bolaño.epub")
        self.assertIs(self._resolve(_md5("Roberto Bolaño - By Night in Chile.epub", "NFD")), nfc_library)

    def test_unswapped_name_with_a_normalization_mismatch_resolves(self):
        book = self._book("b", original=unicodedata.normalize("NFD", "One Hundred Years of Solitude - Gabriel García Márquez.epub"))
        self.assertIs(self._resolve(_md5("One Hundred Years of Solitude - Gabriel García Márquez.epub", "NFC")), book)

    def test_multi_author_device_name_is_a_known_miss(self):
        self._book("omens", original="Good Omens - Terry Pratchett.epub")
        self.assertIsNone(self._resolve(_md5("Terry Pratchett & Neil Gaiman - Good Omens.epub")))

    # ---- skips and guards ----

    def test_names_without_a_separator_are_skipped(self):
        self._book("plain", original="Dune.epub", ebook="cwa_123.epub")
        self.assertIsNone(self._resolve(_md5("Frank Herbert - Dune.epub")))

    def test_unknown_extensions_are_skipped(self):
        self._book("txt", original="Dune - Frank Herbert.txt")
        self.assertIsNone(self._resolve(_md5("Frank Herbert - Dune.txt")))

    def test_non_string_filename_attributes_do_not_raise(self):
        book = MagicMock()
        book.abs_id = "mock"
        self.books["mock"] = book
        self.assertIsNone(self._resolve(_md5("Frank Herbert - Dune.epub")))

    def test_malformed_hash_skips_the_scan(self):
        self._book("bolano", original="By Night in Chile - Roberto Bolaño.epub")
        self.assertIsNone(self._resolve("not-a-hash"))
        self.db.get_books_by_status.assert_not_called()

    # ---- precedence and conflicts ----

    def test_cwa_checksum_match_wins_over_a_filename_variant(self):
        self._book("bolano", original="By Night in Chile - Roberto Bolaño.epub")
        cwa_book = MagicMock(abs_id="cwa", abs_title="cwa")
        with patch.object(kosync_server, "_resolve_book_by_cwa_checksum", return_value=cwa_book):
            self.assertIs(self._resolve("5e27029aefa667a923c4c57f9d33fc48"), cwa_book)
        self.db.get_books_by_status.assert_not_called()

    def test_filename_variants_run_when_cwa_has_no_match(self):
        book = self._book("bolano", original="By Night in Chile - Roberto Bolaño.epub")
        with patch.object(kosync_server, "_resolve_book_by_cwa_checksum", return_value=None) as cwa:
            self.assertIs(self._resolve("5e27029aefa667a923c4c57f9d33fc48"), book)
        cwa.assert_called_once_with("5e27029aefa667a923c4c57f9d33fc48")

    def test_exact_name_wins_over_a_rotation_of_another_book(self):
        self._book("foo-bar", original="Foo - Bar.epub")
        bar_foo = self._book("bar-foo", original="Bar - Foo.epub")
        self.assertIs(self._resolve(_md5("Bar - Foo.epub")), bar_foo)

    def test_same_file_in_both_fields_is_one_match_not_a_conflict(self):
        name = "By Night in Chile - Roberto Bolaño.epub"
        book = self._book("bolano", original=name, ebook=name)
        self.assertIs(self._resolve("5e27029aefa667a923c4c57f9d33fc48"), book)

    def test_two_books_matching_in_the_same_tier_is_a_conflict(self):
        self._book("one", original="Dune - Frank Herbert.epub")
        self._book("two", original="Dune - Frank Herbert.kepub.epub", ebook="Dune - Frank Herbert.epub")
        condition_logger = MagicMock()
        with patch.object(kosync_server, "get_persistent_condition_logger", return_value=condition_logger):
            self.assertIsNone(self._resolve(_md5("Frank Herbert - Dune.epub")))
        condition_logger.warn.assert_called_once()

    def test_conflict_warning_is_resolved_once_the_hash_matches_a_single_book(self):
        self._book("one", original="Dune - Frank Herbert.epub")
        self._book("two", original="Dune - Frank Herbert.kepub.epub", ebook="Dune - Frank Herbert.epub")
        device_hash = _md5("Frank Herbert - Dune.epub")
        condition_logger = MagicMock()
        with patch.object(kosync_server, "get_persistent_condition_logger", return_value=condition_logger):
            self.assertIsNone(self._resolve(device_hash))
            condition_logger.resolve.assert_not_called()
            del self.books["two"]
            self.assertIsNotNone(self._resolve(device_hash))
        key = f"kosync_filename_variant_conflict:{device_hash}"
        self.assertEqual(condition_logger.resolve.call_args.args[1], key)


if __name__ == "__main__":
    unittest.main()
