import unittest
from unittest.mock import MagicMock, patch
import os
import tempfile
import shutil
import zipfile
from src.services.library_service import LibraryService

class TestLibraryPriority(unittest.TestCase):
    def setUp(self):
        self.mock_db = MagicMock()
        self.mock_booklore = MagicMock()
        self.mock_cwa = MagicMock()
        self.mock_abs = MagicMock()
        self.temp_dir = tempfile.mkdtemp()
        self.epub_cache = self.temp_dir
        
        self.service = LibraryService(
            self.mock_db,
            self.mock_booklore,
            self.mock_cwa,
            self.mock_abs,
            self.epub_cache
        )

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    @staticmethod
    def _write_epub(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('mimetype', 'application/epub+zip', compress_type=zipfile.ZIP_STORED)
            archive.writestr('META-INF/container.xml', '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>')
            archive.writestr('OEBPS/content.opf', '<package/>')

    def test_priority_1_abs_direct(self):
        # Setup: ABS has file
        item = {'id': 'item1', 'media': {'metadata': {'title': 'T', 'authorName': 'A'}}}
        self.mock_abs.get_ebook_files.return_value = [{'stream_url': 'url', 'ext': 'epub'}]
        self.mock_abs.download_file.side_effect = lambda _url, path: (self._write_epub(path) or True)
        
        # Act
        result = self.service.acquire_ebook(item)
        
        # Assert
        # Paths on windows might be different separators, using os.path.join
        expected = os.path.join(self.epub_cache, 'item1_direct.epub')
        self.assertEqual(result, expected)
        self.mock_abs.get_ebook_files.assert_called()
        self.mock_cwa.search_ebooks.assert_not_called() # Should stop at P1

    def test_priority_3_cwa(self):
        # Setup: ABS direct match fails
        item = {'id': 'item1', 'media': {'metadata': {'title': 'T', 'authorName': 'A'}}}
        self.mock_abs.get_ebook_files.return_value = []
        
        # CWA configured and finds match
        self.mock_cwa.is_configured.return_value = True
        self.mock_cwa.search_ebooks.return_value = [{'id': 'cwa-1', 'title': 'T', 'author': 'A', 'download_url': 'durl', 'ext': 'epub'}]
        self.mock_cwa.download_ebook.side_effect = lambda _url, path: (self._write_epub(path) or True)
        
        # Act
        result = self.service.acquire_ebook(item)
        
        # Assert
        expected = os.path.join(self.epub_cache, 'item1_cwa.epub')
        self.assertEqual(result, expected)
        self.mock_cwa.search_ebooks.assert_called()
        self.mock_abs.search_ebooks.assert_not_called() # Should stop at P3

    def test_priority_4_abs_search(self):
        # Setup: ABS direct fail, CWA fail
        item = {'id': 'item1', 'media': {'metadata': {'title': 'T', 'authorName': 'A'}}}
        # Mock get_ebook_files for initial check (empty) THEN for search result (found)
        # Side effect iterates through calls
        self.mock_abs.get_ebook_files.side_effect = [[], [{'stream_url': 'surl', 'ext': 'epub'}]]
        
        self.mock_cwa.is_configured.return_value = True
        self.mock_cwa.search_ebooks.return_value = []
        
        # ABS Search finds match
        self.mock_abs.search_ebooks.return_value = [{'id': 'other1', 'author': 'A'}]
        self.mock_abs.download_file.side_effect = lambda _url, path: (self._write_epub(path) or True)
        
        result = self.service.acquire_ebook(item)
        
        expected = os.path.join(self.epub_cache, 'item1_abs_search.epub')
        self.assertEqual(result, expected)
        self.mock_abs.search_ebooks.assert_called()

    def test_priority_4_survives_null_author(self):
        """Diagnostics finding 4185 - ABS sends author: null and acquisition died.

        The reported symptom, which killed the whole book's sync cycle:
            ❌ The Wedding People: 'NoneType' object has no attribute 'lower'
        """
        item = {'id': 'item1', 'media': {'metadata': {'title': 'T', 'authorName': 'A'}}}
        self.mock_abs.get_ebook_files.side_effect = [[], [{'stream_url': 'surl', 'ext': 'epub'}]]

        self.mock_cwa.is_configured.return_value = True
        self.mock_cwa.search_ebooks.return_value = []

        # The key exists, so dict.get()'s default never applies.
        self.mock_abs.search_ebooks.return_value = [{'id': 'other1', 'author': None}]
        self.mock_abs.download_file.side_effect = lambda _url, path: (self._write_epub(path) or True)

        result = self.service.acquire_ebook(item)

        expected = os.path.join(self.epub_cache, 'item1_abs_search.epub')
        self.assertEqual(result, expected)

    def test_fallback(self):
        # All fail
        item = {'id': 'item1', 'media': {'metadata': {'title': 'T', 'authorName': 'A'}}}
        self.mock_abs.get_ebook_files.return_value = []
        self.mock_cwa.is_configured.return_value = True
        self.mock_cwa.search_ebooks.return_value = []
        self.mock_abs.search_ebooks.return_value = []
        
        result = self.service.acquire_ebook(item)
        
        self.assertIsNone(result)
