"""The filesystem hash scan on a REAL database and a REAL EbookParser.

The cache records which KOSYNC_HASH_METHOD produced each row, so after the method
changes the scan neither trusts nor destroys rows of the other method: devices that
still hash the old way keep resolving, and the new hash is found and cached beside it.
"""
import hashlib
import logging
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.api import kosync_server
from src.db.database_service import DatabaseService
from src.db.models import Book, KosyncDocument
from src.utils.ebook_utils import EbookParser

NAME = "By Night in Chile - Roberto Bolaño.epub"
FILENAME_HASH = hashlib.md5(NAME.encode("utf-8")).hexdigest()


class TestHashCacheAcrossMethodSwitch(unittest.TestCase):
    def setUp(self):
        self._saved = (kosync_server._database_service, kosync_server._container)
        self._saved_env = os.environ.get("KOSYNC_HASH_METHOD")
        self.addCleanup(self._restore)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.scan_dir = Path(self.tmp) / "books"
        self.scan_dir.mkdir()
        self.epub = self.scan_dir / NAME
        self.epub.write_bytes(b"epub bytes" * 500)
        self.db = DatabaseService(str(Path(self.tmp) / "db.sqlite"))
        self.addCleanup(self.db.db_manager.close)
        self.container = MagicMock()
        kosync_server._database_service = self.db
        kosync_server._container = self.container
        self.content_hash = self._use("content").get_kosync_id(self.epub)

    def _restore(self):
        kosync_server._database_service, kosync_server._container = self._saved
        if self._saved_env is None:
            os.environ.pop("KOSYNC_HASH_METHOD", None)
        else:
            os.environ["KOSYNC_HASH_METHOD"] = self._saved_env

    def _use(self, method):
        os.environ["KOSYNC_HASH_METHOD"] = method
        parser = EbookParser(self.scan_dir)
        self.container.ebook_parser.return_value = parser
        return parser

    def _scan(self, doc_hash):
        return kosync_server._scan_directory_for_hash(self.scan_dir, doc_hash)

    def test_method_switch_finds_the_new_hash_and_keeps_the_old_row_intact(self):
        self.db.save_book(Book(abs_id="b1", abs_title="B", ebook_filename=NAME, status="active"))
        self._use("content")
        self.assertEqual(self._scan(self.content_hash), NAME)
        self.db.link_kosync_document(self.content_hash, "b1")
        old = self.db.get_kosync_document(self.content_hash)
        self.assertEqual((old.filename, old.hash_method, old.source), (NAME, "content", "filesystem"))

        self._use("filename")
        self.assertEqual(self._scan(FILENAME_HASH), NAME)
        self.assertEqual(self._scan(FILENAME_HASH), NAME)

        old_after = self.db.get_kosync_document(self.content_hash)
        self.assertEqual(
            (old_after.filename, old_after.mtime, old_after.source, old_after.hash_method, old_after.linked_abs_id),
            (NAME, old.mtime, "filesystem", "content", "b1"),
        )
        new = self.db.get_kosync_document(FILENAME_HASH)
        self.assertEqual((new.filename, new.hash_method), (NAME, "filename"))

        sibling_book = kosync_server._resolve_book_by_sibling_hash(FILENAME_HASH)
        self.assertEqual(sibling_book.abs_id, "b1")

    def test_exact_match_of_an_old_method_hash_still_works_after_the_switch(self):
        self._use("content")
        self.assertEqual(self._scan(self.content_hash), NAME)

        parser = self._use("filename")
        with patch.object(parser, "get_kosync_id", wraps=parser.get_kosync_id) as hasher:
            self.assertEqual(self._scan(self.content_hash), NAME)
        hasher.assert_not_called()

    def test_second_scan_does_no_cache_writes(self):
        self._use("content")
        self.assertEqual(self._scan(self.content_hash), NAME)
        with patch.object(self.db, "upsert_kosync_hash_metadata") as upsert, \
                patch.object(self.db, "save_kosync_document") as save:
            self.assertEqual(self._scan(self.content_hash), NAME)
            self._scan("0" * 32)
        upsert.assert_not_called()
        save.assert_not_called()

    def test_changed_file_clears_only_a_row_of_the_same_method(self):
        self._use("content")
        self._scan(self.content_hash)
        self.epub.write_bytes(b"different bytes" * 500)
        os.utime(self.epub, (1, 1))
        new_hash = self._use("content").get_kosync_id(self.epub)

        self.assertEqual(self._scan(new_hash), NAME)

        self.assertIsNone(self.db.get_kosync_document(self.content_hash).filename)
        self.assertEqual(self.db.get_kosync_document(new_hash).filename, NAME)

    def test_changed_file_leaves_a_row_of_the_other_method_alone(self):
        self.db.upsert_kosync_hash_metadata(FILENAME_HASH, NAME, "filesystem", mtime=1.0, hash_method="filename")
        self._use("content")

        self._scan(self.content_hash)

        self.assertEqual(self.db.get_kosync_document(FILENAME_HASH).filename, NAME)

    def test_a_current_mtime_row_of_the_other_method_does_not_hide_the_new_hash(self):
        self.db.upsert_kosync_hash_metadata(
            FILENAME_HASH, NAME, "filesystem", mtime=self.epub.stat().st_mtime, hash_method="filename",
        )
        self._use("content")

        self.assertEqual(self._scan(self.content_hash), NAME)

    def test_cache_write_never_clears_a_stale_row_of_the_other_method(self):
        self.db.upsert_kosync_hash_metadata(FILENAME_HASH, NAME, "filesystem", mtime=1.0, hash_method="filename")
        other_row = self.db.get_kosync_document(FILENAME_HASH)
        self._use("content")

        kosync_server._cache_kosync_metadata(
            self.content_hash, NAME, "booklore", booklore_id="7", cached_doc=other_row,
        )

        self.assertEqual(self.db.get_kosync_document(FILENAME_HASH).filename, NAME)

    def test_cache_writes_never_overwrite_progress(self):
        self.db.save_kosync_document(KosyncDocument(
            document_hash=self.content_hash, percentage=0.42, progress="p", device="kobo",
            device_id="K", user_id=None, linked_abs_id=None,
        ))
        self._use("content")

        self._scan(self.content_hash)

        row = self.db.get_kosync_document(self.content_hash)
        self.assertAlmostEqual(float(row.percentage), 0.42)
        self.assertEqual((row.progress, row.device, row.filename), ("p", "kobo", NAME))

    def test_metadata_cache_writes_do_not_resolve_or_stamp_an_owner(self):
        self._use("content")
        with self.assertNoLogs("src.db.database_service", level=logging.WARNING):
            self._scan("0" * 32)
        self.assertIsNone(self.db.get_kosync_document(self.content_hash).user_id)

    def test_parser_failure_ends_the_directory_scan_without_raising(self):
        self.container.ebook_parser.side_effect = RuntimeError("parser down")
        self.assertIsNone(self._scan(self.content_hash))


if __name__ == "__main__":
    unittest.main()
