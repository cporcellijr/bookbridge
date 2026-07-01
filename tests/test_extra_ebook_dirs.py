"""Tests for EXTRA_EBOOK_DIRS scanning + identifier-based KoSync auto-discovery linking.

Covers the multi-library case where a user's books live outside BOOKS_DIR (e.g. a
separate Calibre library): auto-discovery must hash-scan the extra dir, and link a
hash-matched raw library file to an existing mapping whose ebook is the re-stamped
copy (different bytes/filename, same embedded EPUB identifier).
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.utils.ebook_utils import EbookParser
from src.api import kosync_server


class TestExtraEbookDirsParsing(unittest.TestCase):
    def test_parse_comma_and_newline(self):
        dirs = EbookParser._parse_extra_book_dirs("/a, /b\n/c")
        self.assertEqual(dirs, [Path("/a"), Path("/b"), Path("/c")])

    def test_parse_empty(self):
        self.assertEqual(EbookParser._parse_extra_book_dirs(""), [])
        self.assertEqual(EbookParser._parse_extra_book_dirs("   "), [])

    def test_search_dirs_includes_extra(self):
        with patch.dict(os.environ, {"EXTRA_EBOOK_DIRS": "/x,/y"}):
            parser = EbookParser(books_dir="/books", epub_cache_dir="/cache")
        self.assertEqual(parser.search_dirs(), [Path("/books"), Path("/x"), Path("/y")])

    def test_resolve_book_path_searches_extra_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            books = Path(tmp) / "books"
            books.mkdir()
            extra = Path(tmp) / "calibre" / "Author"
            extra.mkdir(parents=True)
            cache = Path(tmp) / "cache"
            cache.mkdir()
            target = extra / "The Book - Author.epub"
            target.write_bytes(b"epub-bytes")

            with patch.dict(os.environ, {"EXTRA_EBOOK_DIRS": str(Path(tmp) / "calibre")}):
                parser = EbookParser(books_dir=str(books), epub_cache_dir=str(cache))

            self.assertEqual(parser.resolve_book_path("The Book - Author.epub"), target)


class TestNormalizeIdentifier(unittest.TestCase):
    def test_strips_known_prefixes(self):
        n = EbookParser._normalize_identifier
        self.assertEqual(n("urn:uuid:ABC-123"), "abc-123")
        self.assertEqual(n("calibre:38ac490b"), "38ac490b")
        self.assertEqual(n("isbn:9781234567890"), "9781234567890")
        self.assertEqual(n("181b34cf-4a7d"), "181b34cf-4a7d")
        self.assertEqual(n(""), "")
        self.assertEqual(n(None), "")

    def test_raw_and_restamped_share_identifier(self):
        # Real-world shape: raw Calibre file vs the CWA copy that only ADDED a uuid.
        raw = {EbookParser._normalize_identifier("181b34cf-4a7d-4dc2-9645-03badd29e157")}
        cwa = {
            EbookParser._normalize_identifier("181b34cf-4a7d-4dc2-9645-03badd29e157"),
            EbookParser._normalize_identifier("urn:uuid:aa027076-a9e1-4515-81cd-5bd8e244eb6c"),
        }
        self.assertTrue(raw & cwa)


class TestGetBookIdentifiers(unittest.TestCase):
    def test_reads_and_normalizes_dc_identifiers(self):
        parser = EbookParser(books_dir="/books", epub_cache_dir="/cache")
        fake_book = MagicMock()
        fake_book.get_metadata.return_value = [
            ("urn:uuid:AA-BB", {}),
            ("calibre:38ac490b", {}),
        ]
        abs_path = Path(tempfile.gettempdir()) / "book.epub"  # platform-absolute
        with patch("src.utils.ebook_utils.epub.read_epub", return_value=fake_book):
            ids = parser.get_book_identifiers(abs_path)
        self.assertEqual(ids, {"aa-bb", "38ac490b"})

    def test_unreadable_epub_returns_empty(self):
        parser = EbookParser(books_dir="/books", epub_cache_dir="/cache")
        abs_path = Path(tempfile.gettempdir()) / "x.epub"
        with patch("src.utils.ebook_utils.epub.read_epub", side_effect=ValueError("bad")):
            self.assertEqual(parser.get_book_identifiers(abs_path), set())


class TestEbookSearchDirs(unittest.TestCase):
    def setUp(self):
        self._saved = (kosync_server._ebook_dir, kosync_server._container)
        self.addCleanup(self._restore)
        kosync_server._ebook_dir = Path("/books")
        container = MagicMock()
        container.ebook_parser.return_value.extra_book_dirs = [Path("/calibre_library")]
        kosync_server._container = container

    def _restore(self):
        kosync_server._ebook_dir, kosync_server._container = self._saved

    def test_includes_books_dir_and_extra(self):
        self.assertEqual(
            kosync_server._ebook_search_dirs(),
            [Path("/books"), Path("/calibre_library")],
        )


class TestScanDirectoryForHash(unittest.TestCase):
    def setUp(self):
        self._saved = (kosync_server._database_service, kosync_server._container)
        self.addCleanup(self._restore)
        self._db = MagicMock()
        self._db.get_kosync_doc_by_filename.return_value = None
        kosync_server._database_service = self._db
        self._container = MagicMock()
        kosync_server._container = self._container

    def _restore(self):
        kosync_server._database_service, kosync_server._container = self._saved

    def test_returns_filename_on_hash_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            scan = Path(tmp)
            (scan / "Match - Author.epub").write_bytes(b"x")
            self._container.ebook_parser.return_value.get_kosync_id.return_value = "deadbeef"
            with patch.object(kosync_server, "_cache_kosync_metadata"):
                result = kosync_server._scan_directory_for_hash(scan, "deadbeef")
            self.assertEqual(result, "Match - Author.epub")

    def test_returns_none_when_no_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            scan = Path(tmp)
            (scan / "Other.epub").write_bytes(b"x")
            self._container.ebook_parser.return_value.get_kosync_id.return_value = "aaaa"
            with patch.object(kosync_server, "_cache_kosync_metadata"):
                self.assertIsNone(kosync_server._scan_directory_for_hash(scan, "deadbeef"))

    def test_missing_dir_returns_none(self):
        self.assertIsNone(kosync_server._scan_directory_for_hash(Path("/no/such/dir"), "x"))


class TestIdentifierLinking(unittest.TestCase):
    def setUp(self):
        self._saved = (
            kosync_server._database_service,
            kosync_server._container,
            dict(kosync_server._epub_identifier_cache),
        )
        self.addCleanup(self._restore)
        self._db = MagicMock()
        self._parser = MagicMock()
        container = MagicMock()
        container.ebook_parser.return_value = self._parser
        kosync_server._database_service = self._db
        kosync_server._container = container
        kosync_server._epub_identifier_cache.clear()

    def _restore(self):
        db, container, cache = self._saved
        kosync_server._database_service = db
        kosync_server._container = container
        kosync_server._epub_identifier_cache.clear()
        kosync_server._epub_identifier_cache.update(cache)

    @staticmethod
    def _book(abs_id, title, ebook_filename):
        b = MagicMock()
        b.abs_id = abs_id
        b.abs_title = title
        b.ebook_filename = ebook_filename
        b.original_ebook_filename = ebook_filename
        return b

    def test_links_raw_file_to_matched_book_by_shared_identifier(self):
        ids = {
            "The Unhoneymooners - Christina Lauren.epub": {"calibre-uuid-1"},
            "cwa_The_Unhoneymooners.epub": {"calibre-uuid-1", "extra-uuid"},
        }
        self._parser.get_book_identifiers.side_effect = lambda fn: ids.get(str(fn), set())
        self._db.get_kosync_document.return_value = MagicMock(user_id=2)
        book = self._book("abs-1", "The Unhoneymooners", "cwa_The_Unhoneymooners.epub")
        self._db.get_books_by_status.return_value = [book]

        result = kosync_server._resolve_book_by_epub_identifier(
            "The Unhoneymooners - Christina Lauren.epub", doc_id="hash7"
        )

        self.assertIs(result, book)
        self._db.get_books_by_status.assert_called_with("active", user_id=2)

    def test_no_match_when_no_shared_identifier(self):
        ids = {"raw.epub": {"id-a"}, "cwa_other.epub": {"id-b"}}
        self._parser.get_book_identifiers.side_effect = lambda fn: ids.get(str(fn), set())
        self._db.get_kosync_document.return_value = MagicMock(user_id=2)
        self._db.get_books_by_status.return_value = [self._book("abs-2", "Other", "cwa_other.epub")]

        self.assertIsNone(kosync_server._resolve_book_by_epub_identifier("raw.epub", doc_id="h"))

    def test_no_identifiers_short_circuits(self):
        self._parser.get_book_identifiers.return_value = set()
        self.assertIsNone(kosync_server._resolve_book_by_epub_identifier("x.epub", doc_id="h"))
        self._db.get_books_by_status.assert_not_called()


if __name__ == "__main__":
    unittest.main()
