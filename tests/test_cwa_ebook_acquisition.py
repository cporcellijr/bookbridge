"""Acquisition regressions for mapped and discovered CWA/ABS ebooks."""

import os
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock

from src.db.models import Book
from src.services.library_service import LibraryService


def _write_epub(path: str, padding: int = 1500) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>')
        archive.writestr("OEBPS/content.opf", "<package/>")
        archive.writestr("padding", b"x" * padding)


class TestCwaEbookAcquisition(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db = MagicMock()
        self.booklore = MagicMock()
        self.cwa = MagicMock()
        self.abs = MagicMock()
        self.abs.get_ebook_files.return_value = []
        self.abs.search_ebooks.return_value = []
        self.cwa.search_ebooks.return_value = []
        self.service = LibraryService(
            self.db, self.booklore, self.cwa, self.abs, self.temp_dir,
        )

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _mapping(self, filename="Book.epub", source_id="cwa-17", original=None):
        book = MagicMock(spec=Book)
        book.ebook_filename = filename
        book.ebook_source = "CWA"
        book.ebook_source_id = source_id
        book.original_ebook_filename = original
        return book

    @staticmethod
    def _item(title="A Book", author="An Author", item_id="abs-1"):
        return {"id": item_id, "media": {"metadata": {"title": title, "authorName": author}}}

    def _cwa_match(self, title, author="An Author", book_id="found-1"):
        return {"id": book_id, "title": title, "author": author,
                "download_url": f"/download/{book_id}", "ext": "epub"}

    def _download_cwa(self, _url, path):
        _write_epub(path)
        return True

    def test_mapped_cwa_downloads_exact_id_under_original_cache_name(self):
        book = self._mapping("renamed-source.epub", original="device-name.epub")
        self.cwa.is_configured.return_value = True
        self.cwa.get_book_by_id.return_value = {
            "id": "cwa-17", "download_url": "/download/cwa-17", "ext": "epub",
        }
        self.cwa.download_ebook.side_effect = self._download_cwa

        result = self.service.acquire_ebook(None, book)

        self.assertEqual(result, os.path.join(self.temp_dir, "device-name.epub"))
        self.cwa.get_book_by_id.assert_called_once_with("cwa-17")
        self.cwa.download_ebook.assert_called_once_with("/download/cwa-17", result)

    def test_mapped_cwa_cache_is_used_while_client_is_unconfigured(self):
        book = self._mapping("renamed-source.epub", original="device-name.epub")
        cache = os.path.join(self.temp_dir, "device-name.epub")
        _write_epub(cache)
        self.cwa.is_configured.return_value = False

        self.assertEqual(self.service.acquire_ebook(self._item(), book), cache)
        self.cwa.get_book_by_id.assert_not_called()
        self.abs.get_ebook_files.assert_not_called()
        self.abs.search_ebooks.assert_not_called()

    def test_unavailable_explicit_cwa_mapping_fails_closed(self):
        book = self._mapping()
        self.cwa.is_configured.return_value = False

        self.assertIsNone(self.service.acquire_ebook(self._item(), book))
        self.cwa.search_ebooks.assert_not_called()
        self.abs.get_ebook_files.assert_not_called()
        self.abs.search_ebooks.assert_not_called()

    def test_mapped_cwa_rejects_wrong_id_without_downloading(self):
        book = self._mapping(source_id="wanted-id")
        self.cwa.is_configured.return_value = True
        self.cwa.get_book_by_id.return_value = {
            "id": "different-id", "download_url": "/wrong", "ext": "epub",
        }

        self.assertIsNone(self.service.acquire_ebook(None, book))
        self.cwa.get_book_by_id.assert_called_once_with("wanted-id")
        self.cwa.download_ebook.assert_not_called()
        self.abs.search_ebooks.assert_not_called()

    def test_abs_skips_pdf_attachment_and_uses_later_epub(self):
        self.abs.get_ebook_files.return_value = [
            {"stream_url": "/book.pdf", "ext": "pdf"},
            {"stream_url": "/book.epub", "ext": "epub"},
        ]
        self.abs.download_file.side_effect = lambda _url, path: ( _write_epub(path) or True )

        result = self.service.acquire_ebook(self._item())

        self.assertEqual(result, os.path.join(self.temp_dir, "abs-1_direct.epub"))
        self.abs.download_file.assert_called_once_with("/book.epub", result)

    def test_pdf_only_abs_direct_falls_through_to_cwa(self):
        self.abs.get_ebook_files.return_value = [{"stream_url": "/book.pdf", "ext": "pdf"}]
        self.cwa.is_configured.return_value = True
        self.cwa.search_ebooks.side_effect = lambda query: (
            [self._cwa_match("A Book")] if query == "A Book An Author" else []
        )
        self.cwa.download_ebook.side_effect = self._download_cwa

        result = self.service.acquire_ebook(self._item())

        self.assertEqual(result, os.path.join(self.temp_dir, "abs-1_cwa.epub"))
        self.abs.download_file.assert_not_called()

    def test_bad_archive_named_epub_falls_through_to_cwa(self):
        self.abs.get_ebook_files.return_value = [{"stream_url": "/bad.epub", "ext": "epub"}]
        def write_wrong_zip(_url, path):
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("notes.txt", "not an EPUB package")
            return True
        self.abs.download_file.side_effect = write_wrong_zip
        self.cwa.is_configured.return_value = True
        self.cwa.search_ebooks.side_effect = lambda query: (
            [self._cwa_match("A Book")] if query == "A Book An Author" else []
        )
        self.cwa.download_ebook.side_effect = self._download_cwa

        result = self.service.acquire_ebook(self._item())

        self.assertEqual(result, os.path.join(self.temp_dir, "abs-1_cwa.epub"))

    def test_search_retries_article_stripped_title_and_matches_any_parsed_author(self):
        self.cwa.is_configured.return_value = True
        self.cwa.search_ebooks.side_effect = lambda query: (
            [self._cwa_match("The Name of the Wind (Unabridged)", "Neil Gaiman")]
            if query == "Name of the Wind" else []
        )
        self.cwa.download_ebook.side_effect = self._download_cwa

        result = self.service.acquire_ebook(self._item(
            "The Name of the Wind (Unabridged)", "Terry Pratchett, Neil Gaiman",
        ))

        self.assertEqual(result, os.path.join(self.temp_dir, "abs-1_cwa.epub"))
        self.assertIn("Name of the Wind", [entry.args[0] for entry in self.cwa.search_ebooks.call_args_list])

    def test_search_rejects_ambiguous_or_wrong_author_results(self):
        self.cwa.is_configured.return_value = True
        self.cwa.search_ebooks.side_effect = lambda _query: [
            self._cwa_match("A Book", "Wrong Author", "wrong-author"),
            self._cwa_match("A Book", "An Author", "candidate-1"),
            self._cwa_match("A Book", "An Author", "candidate-2"),
        ]

        self.assertIsNone(self.service.acquire_ebook(self._item()))
        self.cwa.download_ebook.assert_not_called()

    def test_search_keeps_subtitles_and_meaningful_parenthetical_qualifiers_distinct(self):
        self.cwa.is_configured.return_value = True
        self.cwa.search_ebooks.side_effect = lambda _query: [
            self._cwa_match("Saga: Volume Two"),
            self._cwa_match("Dune"),
        ]

        self.assertIsNone(self.service.acquire_ebook(self._item("Saga: Volume One")))
        self.assertIsNone(self.service.acquire_ebook(self._item("Dune (Illustrated Edition)")))
        self.cwa.download_ebook.assert_not_called()

    def test_search_allows_catalogue_title_without_source_subtitle(self):
        self.cwa.is_configured.return_value = True
        self.cwa.search_ebooks.side_effect = lambda query: (
            [self._cwa_match("Saga")] if query == "Saga" else []
        )
        self.cwa.download_ebook.side_effect = self._download_cwa

        result = self.service.acquire_ebook(self._item("Saga: Volume One"))

        self.assertEqual(result, os.path.join(self.temp_dir, "abs-1_cwa.epub"))

    def test_search_does_not_allow_subtitle_fallback_without_author(self):
        self.cwa.is_configured.return_value = True
        self.cwa.search_ebooks.side_effect = lambda _query: [
            self._cwa_match("Saga: Volume One", "An Author"),
        ]

        self.assertIsNone(self.service.acquire_ebook(self._item("Saga", author="")))
        self.cwa.download_ebook.assert_not_called()

    def test_later_exact_volume_match_beats_earlier_base_title(self):
        broad = self._cwa_match("Saga", "An Author", "series-root")
        exact = self._cwa_match("Saga: Volume One", "An Author", "volume-one")
        self.cwa.is_configured.return_value = True
        self.cwa.search_ebooks.side_effect = lambda query: (
            [broad] if query == "Saga: Volume One An Author" else
            [exact] if query == "Saga: Volume One" else []
        )
        self.cwa.download_ebook.side_effect = self._download_cwa

        result = self.service.acquire_ebook(self._item("Saga: Volume One"))

        self.assertEqual(result, os.path.join(self.temp_dir, "abs-1_cwa.epub"))
        self.cwa.download_ebook.assert_called_once_with(exact["download_url"], result)

    def test_exact_matches_found_across_queries_remain_ambiguous(self):
        first = self._cwa_match("A Book", "An Author", "candidate-1")
        second = self._cwa_match("A Book", "An Author", "candidate-2")
        self.cwa.is_configured.return_value = True
        self.cwa.search_ebooks.side_effect = lambda query: (
            [first] if query == "A Book An Author" else
            [second] if query == "A Book" else []
        )

        self.assertIsNone(self.service.acquire_ebook(self._item()))
        self.cwa.download_ebook.assert_not_called()


if __name__ == "__main__":
    unittest.main()
