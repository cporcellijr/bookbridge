import os
import shutil
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from src.db.database_service import DatabaseService
from src.db.models import Book
from src.services.koreader_device_sync_service import KOReaderDeviceSyncService


TEST_DIR = "/tmp/test_koreader_device_sync_service"


class TestKOReaderDeviceSyncService(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.path.exists(TEST_DIR):
            shutil.rmtree(TEST_DIR)
        os.makedirs(TEST_DIR, exist_ok=True)
        cls.db = DatabaseService(os.path.join(TEST_DIR, "test.db"))

    def setUp(self):
        with self.db.get_session() as session:
            session.query(Book).delete()

        self.books_dir = Path(TEST_DIR) / "books"
        self.cache_dir = Path(TEST_DIR) / "epub_cache"
        self.books_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        ebook_parser = MagicMock()

        def resolve_book_path(filename):
            candidate = self.books_dir / filename
            if candidate.exists():
                return candidate
            cached = self.cache_dir / filename
            if cached.exists():
                return cached
            raise FileNotFoundError(filename)

        ebook_parser.resolve_book_path.side_effect = resolve_book_path
        ebook_parser.get_kosync_id.side_effect = lambda filepath: f"hash-{Path(filepath).stem}"

        self.service = KOReaderDeviceSyncService(
            database_service=self.db,
            ebook_parser=ebook_parser,
            abs_client=MagicMock(),
            booklore_client=MagicMock(),
            cwa_client=MagicMock(),
            kavita_client=MagicMock(),
            epub_cache_dir=self.cache_dir,
        )

    def _write_book_file(self, filename: str, content: bytes = b"epub") -> Path:
        path = self.books_dir / filename
        path.write_bytes(content)
        return path

    def test_manifest_prefers_original_non_storyteller_filename(self):
        self._write_book_file("kavita_187.epub")
        book = Book(
            abs_id="abs-1",
            abs_title="Dragon's Justice",
            ebook_filename="storyteller_abc.epub",
            original_ebook_filename="kavita_187.epub",
            kosync_doc_id="hash-1",
            status="active",
        )
        self.db.save_book(book)

        manifest = self.service.build_manifest()
        self.assertEqual(len(manifest["books"]), 1)
        item = manifest["books"][0]
        self.assertEqual(item["abs_id"], "abs-1")
        self.assertEqual(item["content_hash"], "hash-kavita_187")
        self.assertEqual(item["download_path"], "/koreader/device-sync/books/abs-1/download")
        self.assertEqual(item["filename"], "Dragon's Justice.epub")

    def test_manifest_adds_suffix_for_filename_collisions(self):
        self._write_book_file("kavita_1.epub")
        self._write_book_file("kavita_2.epub")
        self.db.save_book(
            Book(
                abs_id="abs-a",
                abs_title="Same Title",
                original_ebook_filename="kavita_1.epub",
                kosync_doc_id="hash-a",
                status="active",
            )
        )
        self.db.save_book(
            Book(
                abs_id="abs-b",
                abs_title="Same Title",
                original_ebook_filename="kavita_2.epub",
                kosync_doc_id="hash-b",
                status="active",
            )
        )

        manifest = self.service.build_manifest()
        filenames = sorted(item["filename"] for item in manifest["books"])
        self.assertEqual(
            filenames,
            ["Same Title__abs-a.epub", "Same Title__abs-b.epub"],
        )

    def test_resolve_download_uses_local_original_file(self):
        source_path = self.books_dir / "kavita_187.epub"
        source_path.write_bytes(b"epub")
        self.db.save_book(
            Book(
                abs_id="abs-1",
                abs_title="Dragon's Justice",
                original_ebook_filename="kavita_187.epub",
                kosync_doc_id="hash-1",
                status="active",
            )
        )

        resolved = self.service.resolve_download("abs-1")
        self.assertIsNotNone(resolved)
        self.assertEqual(Path(resolved["path"]), source_path)
        self.assertEqual(resolved["filename"], "Dragon's Justice.epub")
        self.assertEqual(resolved["content_hash"], "hash-kavita_187")
        self.assertEqual(resolved["mime_type"], "application/epub+zip")

    def test_manifest_includes_shelves_from_mapping(self):
        self._write_book_file("horror.epub")
        self.db.save_book(
            Book(
                abs_id="abs-1",
                abs_title="Horror Book",
                original_ebook_filename="horror.epub",
                kosync_doc_id="hash-1",
                ebook_source="booklore",
                ebook_source_id="42",
                status="active",
            )
        )

        shelf_mapping = {"42": ["Sci-fi Horror", "Dark Fiction"]}
        manifest = self.service.build_manifest(shelf_mapping=shelf_mapping)
        self.assertEqual(len(manifest["books"]), 1)
        item = manifest["books"][0]
        self.assertEqual(item["shelves"], ["Sci-fi Horror", "Dark Fiction"])

    def test_manifest_no_shelves_when_disabled(self):
        self._write_book_file("plain.epub")
        self.db.save_book(
            Book(
                abs_id="abs-1",
                abs_title="Plain Book",
                original_ebook_filename="plain.epub",
                kosync_doc_id="hash-1",
                status="active",
            )
        )

        manifest = self.service.build_manifest()
        self.assertEqual(len(manifest["books"]), 1)
        item = manifest["books"][0]
        self.assertNotIn("shelves", item)

    def test_manifest_uses_unsorted_shelf_for_unmatched_book(self):
        self._write_book_file("unshelved.epub")
        self.db.save_book(
            Book(
                abs_id="abs-1",
                abs_title="Unshelved Book",
                original_ebook_filename="unshelved.epub",
                kosync_doc_id="hash-1",
                ebook_source="booklore",
                ebook_source_id="99",
                status="active",
            )
        )

        shelf_mapping = {"42": ["Fantasy"]}
        manifest = self.service.build_manifest(shelf_mapping=shelf_mapping)
        self.assertEqual(len(manifest["books"]), 1)
        item = manifest["books"][0]
        self.assertEqual(item["shelves"], ["Unsorted"])

    def test_manifest_uses_unsorted_shelf_when_source_id_missing(self):
        self._write_book_file("no-source.epub")
        self.db.save_book(
            Book(
                abs_id="abs-1",
                abs_title="No Source Book",
                original_ebook_filename="no-source.epub",
                kosync_doc_id="hash-1",
                status="active",
            )
        )

        manifest = self.service.build_manifest(shelf_mapping={"42": ["Fantasy"]})
        self.assertEqual(len(manifest["books"]), 1)
        item = manifest["books"][0]
        self.assertEqual(item["shelves"], ["Unsorted"])

    def test_manifest_and_download_use_resolved_cached_artifact_hash(self):
        self.service.booklore_client.is_configured.return_value = True
        self.service.booklore_client.download_book.return_value = b"remote-epub"

        self.db.save_book(
            Book(
                abs_id="abs-1",
                abs_title="Remote Book",
                original_ebook_filename="remote.epub",
                kosync_doc_id="stale-hash",
                ebook_source="booklore",
                ebook_source_id="42",
                status="active",
            )
        )

        manifest = self.service.build_manifest()
        self.assertEqual(len(manifest["books"]), 1)
        self.assertEqual(manifest["books"][0]["content_hash"], "hash-remote")

        resolved = self.service.resolve_download("abs-1")
        self.assertIsNotNone(resolved)
        self.assertEqual(Path(resolved["path"]), self.cache_dir / "remote.epub")
        self.assertEqual(resolved["content_hash"], "hash-remote")

