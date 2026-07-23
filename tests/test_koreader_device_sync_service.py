import os
import shutil
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from src.db.database_service import DatabaseService
from src.db.models import Book, KosyncDocument
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
            session.query(KosyncDocument).delete()

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
            bookorbit_client=MagicMock(),
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

    def test_manifest_excludes_audiobook_only_book_without_warning(self):
        """Audiobook-only mappings have no ebook file by design and must not be
        pulled into the ebook device-sync manifest -- previously every cycle
        logged a spurious "no original ebook filename" warning for them,
        forever, since such a book can never satisfy that check.
        """
        self._write_book_file("kavita_187.epub")
        ebook_book = Book(
            abs_id="abs-ebook-1",
            abs_title="Dragon's Justice",
            ebook_filename="kavita_187.epub",
            kosync_doc_id="hash-1",
            status="active",
        )
        audio_only_book = Book(
            abs_id="abs-audio-only-1",
            abs_title="Exiles",
            sync_mode="audiobook_only",
            kosync_doc_id="forging_abs-audio-only-1",
            status="active",
        )
        self.db.save_book(ebook_book)
        self.db.save_book(audio_only_book)

        with self.assertNoLogs("src.services.koreader_device_sync_service", level="WARNING"):
            manifest = self.service.build_manifest()

        self.assertEqual(len(manifest["books"]), 1)
        self.assertEqual(manifest["books"][0]["abs_id"], "abs-ebook-1")

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

    def test_manifest_revision_changes_when_shelves_change(self):
        base_item = {
            "abs_id": "abs-1",
            "filename": "Book.epub",
            "content_hash": "hash-1",
            "size": 123,
        }

        without_shelves = self.service._compute_revision([dict(base_item)])
        with_shelves = self.service._compute_revision([
            {**base_item, "shelves": ["Owned", "Sci-Fi"]}
        ])

        self.assertNotEqual(without_shelves, with_shelves)

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

        # A primary may intentionally identify another EPUB build. Keep it stable
        # while linking the hash of the bytes actually served by the manifest.
        self.assertEqual(self.db.get_book("abs-1").kosync_doc_id, "stale-hash")
        served_doc = self.db.get_kosync_document("hash-remote")
        self.assertIsNotNone(served_doc)
        self.assertEqual(served_doc.linked_abs_id, "abs-1")

        resolved = self.service.resolve_download("abs-1")
        self.assertIsNotNone(resolved)
        self.assertEqual(Path(resolved["path"]), self.cache_dir / "remote.epub")
        self.assertEqual(resolved["content_hash"], "hash-remote")

    def test_manifest_downloads_bookorbit_source_into_epub_cache(self):
        self.service.bookorbit_client.is_configured.return_value = True
        self.service.bookorbit_client.download_book.return_value = b"bookorbit-epub"

        self.db.save_book(
            Book(
                abs_id="abs-1",
                abs_title="Jackknife",
                original_ebook_filename="Jackknife.epub",
                kosync_doc_id="stale-hash",
                ebook_source="BookOrbit",
                ebook_source_id="42",
                status="active",
            )
        )

        manifest = self.service.build_manifest()

        self.assertEqual(len(manifest["books"]), 1)
        self.assertEqual(manifest["books"][0]["content_hash"], "hash-Jackknife")
        self.assertEqual((self.cache_dir / "Jackknife.epub").read_bytes(), b"bookorbit-epub")
        self.service.bookorbit_client.download_book.assert_called_once_with("42")

    def test_matching_stored_hash_is_not_rewritten(self):
        self._write_book_file("kavita_187.epub")
        self.db.save_book(
            Book(
                abs_id="abs-1",
                abs_title="Already Correct",
                original_ebook_filename="kavita_187.epub",
                kosync_doc_id="hash-kavita_187",
                status="active",
            )
        )

        self.service.build_manifest()

        self.assertEqual(self.db.get_book("abs-1").kosync_doc_id, "hash-kavita_187")

    def test_served_hash_linked_as_sibling_even_when_matching(self):
        self._write_book_file("kavita_187.epub")
        self.db.save_book(
            Book(
                abs_id="abs-1",
                abs_title="Already Correct",
                original_ebook_filename="kavita_187.epub",
                kosync_doc_id="hash-kavita_187",
                status="active",
            )
        )

        self.service.build_manifest()

        # The served file's hash is now durably linked, so a BridgeSync device that
        # downloaded it resolves to the book via the document link (not just the column).
        served_doc = self.db.get_kosync_document("hash-kavita_187")
        self.assertIsNotNone(served_doc)
        self.assertEqual(served_doc.linked_abs_id, "abs-1")

    def test_drifted_primary_hash_preserved_with_served_hash_as_sibling(self):
        self._write_book_file("kavita_187.epub")
        self.db.save_book(
            Book(
                abs_id="abs-1",
                abs_title="Drifted",
                original_ebook_filename="kavita_187.epub",
                kosync_doc_id="stale-hash",
                status="active",
            )
        )

        self.service.build_manifest()

        self.assertEqual(self.db.get_book("abs-1").kosync_doc_id, "stale-hash")
        stale_doc = self.db.get_kosync_document("stale-hash")
        self.assertIsNotNone(stale_doc)
        self.assertEqual(stale_doc.linked_abs_id, "abs-1")
        served_doc = self.db.get_kosync_document("hash-kavita_187")
        self.assertIsNotNone(served_doc)
        self.assertEqual(served_doc.linked_abs_id, "abs-1")

    def test_active_device_hash_remains_primary(self):
        self._write_book_file("kavita_187.epub")
        self.db.save_book(
            Book(
                abs_id="abs-1",
                abs_title="Forged Book",
                original_ebook_filename="kavita_187.epub",
                kosync_doc_id="device-forged-hash",
                status="active",
            )
        )
        # A real reader (KOReader 'go7') is actively syncing against the forged-EPUB hash.
        self.db.save_kosync_document(
            KosyncDocument(
                document_hash="device-forged-hash",
                percentage=0.5,
                device="go7",
                device_id="go7",
                linked_abs_id="abs-1",
            )
        )

        self.service.build_manifest()

        # Device activity is not needed to protect the selected primary hash.
        self.assertEqual(self.db.get_book("abs-1").kosync_doc_id, "device-forged-hash")
        # The served-file hash is still linked as a sibling for BridgeSync devices.
        served_doc = self.db.get_kosync_document("hash-kavita_187")
        self.assertIsNotNone(served_doc)
        self.assertEqual(served_doc.linked_abs_id, "abs-1")

    def test_internal_bot_hash_remains_primary(self):
        self._write_book_file("kavita_187.epub")
        self.db.save_book(
            Book(
                abs_id="abs-1",
                abs_title="Bot Only",
                original_ebook_filename="kavita_187.epub",
                kosync_doc_id="stale-hash",
                status="active",
            )
        )
        # Internal activity does not make the primary hash eligible for replacement.
        self.db.save_kosync_document(
            KosyncDocument(
                document_hash="stale-hash",
                percentage=0.5,
                device="abs-sync-bot",
                device_id="abs-sync-bot",
                linked_abs_id="abs-1",
            )
        )

        self.service.build_manifest()

        self.assertEqual(self.db.get_book("abs-1").kosync_doc_id, "stale-hash")
        served_doc = self.db.get_kosync_document("hash-kavita_187")
        self.assertIsNotNone(served_doc)
        self.assertEqual(served_doc.linked_abs_id, "abs-1")

    def test_manifest_rebuild_is_idempotent_for_drifted_primary(self):
        self._write_book_file("kavita_187.epub")
        self.db.save_book(
            Book(
                abs_id="abs-1",
                abs_title="Pinned",
                original_ebook_filename="kavita_187.epub",
                kosync_doc_id="pinned-hash",
                status="active",
            )
        )

        self.service.build_manifest()
        self.service.build_manifest()

        self.assertEqual(self.db.get_book("abs-1").kosync_doc_id, "pinned-hash")
        linked_hashes = {
            doc.document_hash
            for doc in self.db.get_kosync_documents_for_book("abs-1")
        }
        self.assertEqual(linked_hashes, {"pinned-hash", "hash-kavita_187"})

    def test_bare_primary_hash_is_linked_during_manifest_build(self):
        self._write_book_file("kavita_187.epub")
        self.db.save_book(
            Book(
                abs_id="abs-1",
                abs_title="Bare Primary",
                original_ebook_filename="kavita_187.epub",
                kosync_doc_id="bare-primary-hash",
                status="active",
            )
        )

        self.service.build_manifest()

        primary_doc = self.db.get_kosync_document("bare-primary-hash")
        self.assertIsNotNone(primary_doc)
        self.assertEqual(primary_doc.linked_abs_id, "abs-1")

    def test_ensure_linked_kosync_document_upserts_and_relinks(self):
        # Creates a row when missing.
        self.assertTrue(self.db.ensure_linked_kosync_document("h1", "abs-1"))
        doc = self.db.get_kosync_document("h1")
        self.assertIsNotNone(doc)
        self.assertEqual(doc.linked_abs_id, "abs-1")
        # No-op when already linked to the same book.
        self.assertFalse(self.db.ensure_linked_kosync_document("h1", "abs-1"))
        # Relinks when pointing elsewhere.
        self.assertTrue(self.db.ensure_linked_kosync_document("h1", "abs-2"))
        self.assertEqual(self.db.get_kosync_document("h1").linked_abs_id, "abs-2")

    def test_content_hash_not_recomputed_when_ebook_unchanged(self):
        """Regression for #342: the 60s prebuilder loop must not re-hash
        unchanged ebook files. The mtime/size cache should serve the second
        build_manifest() call without calling get_kosync_id again."""
        self._write_book_file("kavita_187.epub")
        self.db.save_book(
            Book(
                abs_id="abs-1",
                abs_title="Cached Book",
                original_ebook_filename="kavita_187.epub",
                kosync_doc_id="hash-1",
                status="active",
            )
        )

        self.service.build_manifest()
        self.service.build_manifest()

        self.assertEqual(
            self.service.ebook_parser.get_kosync_id.call_count,
            1,
            "Second manifest build should be served from mtime cache (issue #342)",
        )

    def test_content_hash_recomputed_when_ebook_changes(self):
        """A changed ebook file (different size) must be re-hashed on the next
        manifest build."""
        path = self._write_book_file("changing.epub", content=b"epub-v1")
        self.db.save_book(
            Book(
                abs_id="abs-1",
                abs_title="Changing Book",
                original_ebook_filename="changing.epub",
                kosync_doc_id="hash-1",
                status="active",
            )
        )

        self.service.build_manifest()

        # Rewrite with clearly different content AND different size to guarantee
        # a cache miss regardless of mtime granularity.
        path.write_bytes(b"epub-v2-much-longer-body")
        os.utime(path, None)  # bump mtime

        self.service.build_manifest()

        self.assertEqual(
            self.service.ebook_parser.get_kosync_id.call_count,
            2,
            "Changed file (different size) must be re-hashed",
        )

