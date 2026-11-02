import os
import shutil
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

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
        ebook_parser.hash_method = "content"
        ebook_parser.get_kosync_id.side_effect = lambda filepath: f"hash-{Path(filepath).stem}"
        ebook_parser.get_kosync_content_id.side_effect = lambda filepath: f"hash-{Path(filepath).stem}"

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
        self.assertEqual(item["size"], 4)
        self.assertEqual(item["filename"], "Dragon's Justice.epub")

    def test_manifest_uses_storyteller_artifact_for_ebook_only_mode(self):
        """Regression for #1375: when sync_mode is 'ebook_only' and the only
        available filename is a Storyteller artifact, it should be used instead
        of emitting a warning and skipping the book."""
        self._write_book_file("storyteller_abc.epub")
        book = Book(
            abs_id="abs-ebook-only-1",
            abs_title="Storyteller Only Book",
            ebook_filename="storyteller_abc.epub",
            original_ebook_filename=None,
            sync_mode="ebook_only",
            kosync_doc_id="hash-1",
            status="active",
        )
        self.db.save_book(book)

        with self.assertNoLogs("src.services.koreader_device_sync_service", level="WARNING"):
            manifest = self.service.build_manifest()

        self.assertEqual(len(manifest["books"]), 1)
        item = manifest["books"][0]
        self.assertEqual(item["abs_id"], "abs-ebook-only-1")
        self.assertEqual(item["content_hash"], "hash-storyteller_abc")
        self.assertEqual(item["download_path"], "/koreader/device-sync/books/abs-ebook-only-1/download")
        self.assertEqual(item["filename"], "Storyteller Only Book.epub")

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

    def test_one_unstattable_file_does_not_lose_the_whole_manifest(self):
        """A file deleted between resolution and sizing must cost only its own size.

        The cache-cleanup paths delete orphaned EPUBs concurrently. An unguarded
        stat() raised out of the whole build_manifest loop, so one vanished file
        left every book without a manifest entry.
        """
        self._write_book_file("kavita_1.epub")
        self._write_book_file("kavita_2.epub")
        for abs_id, filename in (("abs-a", "kavita_1.epub"), ("abs-b", "kavita_2.epub")):
            self.db.save_book(
                Book(
                    abs_id=abs_id,
                    abs_title=f"Book {abs_id}",
                    original_ebook_filename=filename,
                    kosync_doc_id=f"hash-{abs_id}",
                    status="active",
                )
            )

        # Reproduce the real race: the file is present when the artifact is
        # resolved and gone by the time the manifest item is sized.
        original_resolve = self.service._resolve_download_artifact

        def _resolve_then_delete(book, *args, **kwargs):
            resolved = original_resolve(book, *args, **kwargs)
            if resolved and str(getattr(book, "abs_id", "")) == "abs-a":
                Path(resolved["path"]).unlink()
            return resolved

        with patch.object(
            self.service, "_resolve_download_artifact", side_effect=_resolve_then_delete
        ):
            manifest = self.service.build_manifest()

        sizes = {item["abs_id"]: item["size"] for item in manifest["books"]}
        self.assertEqual(len(sizes), 2, "both books must still be in the manifest")
        self.assertIsNone(sizes["abs-a"], "the un-stat-able book reports no size")
        self.assertEqual(sizes["abs-b"], len(b"epub"))

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
        # Compare resolved: a hosted copy now comes back from safe_cache_path, which
        # resolves for traversal safety. Identical on POSIX; on Windows an unresolved
        # "/tmp/..." expectation picks up a drive letter and only looks different.
        self.assertEqual(
            Path(resolved["path"]).resolve(),
            (self.cache_dir / "remote.epub").resolve(),
        )
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

    def test_manifest_downloads_kavita_source_by_mapped_chapter_id(self):
        """Natural Kavita filenames still resolve through ebook_source_id."""
        self.service.bookorbit_client.is_configured.return_value = False
        self.service.booklore_client.is_configured.return_value = False
        self.service.abs_client.is_configured.return_value = False
        self.service.cwa_client.is_configured.return_value = False
        self.service.kavita_client.is_configured.return_value = True
        self.service.kavita_client.download_book.return_value = b"kavita-epub"

        self.db.save_book(
            Book(
                abs_id="abs-kavita-1",
                abs_title="The Example",
                original_ebook_filename="The Example.epub",
                kosync_doc_id="stale-hash",
                ebook_source="Kavita",
                ebook_source_id="73",
                status="active",
            )
        )

        manifest = self.service.build_manifest()

        self.assertEqual(len(manifest["books"]), 1)
        self.assertEqual(manifest["books"][0]["content_hash"], "hash-The Example")
        self.assertEqual((self.cache_dir / "The Example.epub").read_bytes(), b"kavita-epub")
        self.service.kavita_client.download_book.assert_called_once_with("73")
        self.service.kavita_client.find_book_by_filename.assert_not_called()

    def test_manifest_downloads_natural_filename_from_abs_id(self):
        """ABS uploads need not use the legacy ``{item_id}_abs.epub`` filename."""
        self.service.bookorbit_client.is_configured.return_value = False
        self.service.booklore_client.is_configured.return_value = False
        self.service.cwa_client.is_configured.return_value = False
        self.service.kavita_client.is_configured.return_value = False
        self.service.abs_client.is_configured.return_value = True
        self.service.abs_client.get_ebook_files.return_value = [
            {"ext": "epub", "stream_url": "https://abs.invalid/book.epub"}
        ]

        def download_file(_url, output_path):
            Path(output_path).write_bytes(b"abs-epub")
            return True

        self.service.abs_client.download_file.side_effect = download_file
        self.db.save_book(
            Book(
                abs_id="abs-item-42",
                abs_title="The Big Book of Cyberpunk Vol. I",
                original_ebook_filename=(
                    "The Big Book of Cyberpunk Vol. I - Jared Shurin.epub"
                ),
                kosync_doc_id="stale-hash",
                status="active",
            )
        )

        with self.assertNoLogs(
            "src.services.koreader_device_sync_service",
            level="WARNING",
        ):
            manifest = self.service.build_manifest()

        self.assertEqual(len(manifest["books"]), 1)
        self.service.abs_client.get_ebook_files.assert_called_once_with("abs-item-42")
        self.assertEqual(
            (
                self.cache_dir
                / "The Big Book of Cyberpunk Vol. I - Jared Shurin.epub"
            ).read_bytes(),
            b"abs-epub",
        )

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
            self.service.ebook_parser.get_kosync_content_id.call_count,
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
            self.service.ebook_parser.get_kosync_content_id.call_count,
            2,
            "Changed file (different size) must be re-hashed",
        )

    def _use_filename_hashing(self):
        parser = self.service.ebook_parser
        parser.hash_method = "filename"
        parser.get_kosync_id.side_effect = lambda filepath: f"name-{Path(filepath).name}"
        parser.get_kosync_content_id.side_effect = lambda filepath: f"content-{Path(filepath).stem}"

    def test_filename_mode_manifest_carries_the_content_hash(self):
        """BridgeSync checks each download against its content hash, so a filename
        hash in the manifest fails every download."""
        self._use_filename_hashing()
        self._write_book_file("kavita_187.epub")
        self.db.save_book(
            Book(
                abs_id="abs-1",
                abs_title="Dragon's Justice",
                original_ebook_filename="kavita_187.epub",
                status="active",
            )
        )

        manifest = self.service.build_manifest()
        resolved = self.service.resolve_download("abs-1")

        self.assertEqual(manifest["books"][0]["content_hash"], "content-kavita_187")
        self.assertEqual(resolved["content_hash"], "content-kavita_187")

    def test_filename_mode_links_the_filename_hash_for_progress(self):
        """Devices on filename hashing report the filename's hash over KoSync, so
        that is the hash linked to the book, not the content hash."""
        self._use_filename_hashing()
        self._write_book_file("kavita_187.epub")
        self.db.save_book(
            Book(
                abs_id="abs-1",
                abs_title="Dragon's Justice",
                original_ebook_filename="kavita_187.epub",
                status="active",
            )
        )

        self.service.build_manifest()

        self.assertEqual(
            self.db.get_kosync_document("name-Dragon's Justice.epub").linked_abs_id, "abs-1"
        )

    def test_filename_mode_links_the_content_hash_for_reader_statistics(self):
        """KOReader's statistics, reading statuses and highlights name a book by its
        content hash in either hash method, so the served file's content hash is
        linked too."""
        self._use_filename_hashing()
        self._write_book_file("kavita_187.epub")
        self.db.save_book(
            Book(
                abs_id="abs-1",
                abs_title="Dragon's Justice",
                original_ebook_filename="kavita_187.epub",
                status="active",
            )
        )

        self.service.build_manifest()

        self.assertEqual(self.db.get_kosync_document("content-kavita_187").linked_abs_id, "abs-1")

    def test_filename_mode_shared_content_hash_keeps_its_owner_across_reconcile_and_manifest(self):
        """Two books served from one file share its content hash. Reconcile gives it
        to the first book; building the manifest must not move it to the second."""
        self._use_filename_hashing()
        self._write_book_file("shared.epub")
        for abs_id, title in (("abs-a", "Book A"), ("abs-b", "Book B")):
            self.db.save_book(Book(abs_id=abs_id, abs_title=title,
                                   original_ebook_filename="shared.epub", status="active"))

        owners = []
        for _ in range(2):
            self.service.reconcile_hashes()
            owners.append(self.db.get_kosync_document("content-shared").linked_abs_id)
            self.service.build_manifest()
            owners.append(self.db.get_kosync_document("content-shared").linked_abs_id)

        self.assertEqual(len(set(owners)), 1, owners)

    def test_filename_mode_reconcile_binds_the_filename_hash(self):
        self._use_filename_hashing()
        self._write_book_file("kavita_187.epub")
        self.db.save_book(
            Book(
                abs_id="abs-1",
                abs_title="Dragon's Justice",
                original_ebook_filename="kavita_187.epub",
                status="active",
            )
        )

        summary = self.service.reconcile_hashes()

        self.assertEqual(summary["linked"], 2)
        self.assertEqual(
            self.db.get_kosync_document("name-Dragon's Justice.epub").linked_abs_id, "abs-1"
        )
        self.assertEqual(self.db.get_kosync_document("content-kavita_187").linked_abs_id, "abs-1")

    def test_filename_mode_reconcile_uses_global_filename_map_across_users(self):
        """Reader B adding another "Home" must not take the hash of reader A's Home.epub."""
        self._use_filename_hashing()
        self._write_book_file("HomeA.epub", content=b"epubA")
        self._write_book_file("HomeB.epub", content=b"epubB")

        user_a = self.db.create_user("userA", password="p")
        user_b = self.db.create_user("userB", password="p")

        self.db.save_book(Book(abs_id="abs-a", abs_title="Home", original_ebook_filename="HomeA.epub", status="active"))
        self.db.save_book(Book(abs_id="abs-b", abs_title="Home", original_ebook_filename="HomeB.epub", status="active"))

        self.db.link_user_book(user_a.id, "abs-a")
        self.db.link_user_book(user_b.id, "abs-b")

        # Reader A's hash already exists for the global manifest name
        self.db.ensure_linked_kosync_document("name-Home.epub", "abs-a")

        original_user_id = self.service.user_id
        try:
            self.service.user_id = user_b.id
            self.service.reconcile_hashes()
        finally:
            self.service.user_id = original_user_id

        # A's hash must still be linked to A's book
        self.assertEqual(self.db.get_kosync_document("name-Home.epub").linked_abs_id, "abs-a")
        # B's hash must be the suffixed global name, not the plain one
        self.assertEqual(
            self.db.get_kosync_document("name-Home__abs-b.epub").linked_abs_id, "abs-b"
        )
        # Ensure we didn't bind A's plain hash to B
        doc = self.db.get_kosync_document("name-Home.epub")
        self.assertIsNotNone(doc)
        self.assertNotEqual(doc.linked_abs_id, "abs-b")

    def test_content_mode_shared_file_hash_stays_with_first_owner_and_second_manifest_skipped(self):
        """Two books share one EPUB in content mode. The first owner keeps the hash;
        repeated manifests/downloads/reconcile must not steal it, and the second book
        must not appear in the manifest/download once the conflict exists."""
        self._write_book_file("shared.epub")
        self.db.save_book(Book(abs_id="abs-a", abs_title="Book A",
                               original_ebook_filename="shared.epub", status="active"))
        self.db.save_book(Book(abs_id="abs-b", abs_title="Book B",
                               original_ebook_filename="shared.epub", status="active"))

        manifest = self.service.build_manifest()
        winner = self.db.get_kosync_document("hash-shared").linked_abs_id
        self.assertEqual(len(manifest["books"]), 1)
        self.assertEqual(manifest["books"][0]["abs_id"], winner)

        owners = []
        for _ in range(3):
            self.service.reconcile_hashes()
            owners.append(self.db.get_kosync_document("hash-shared").linked_abs_id)
            self.service.build_manifest()
            owners.append(self.db.get_kosync_document("hash-shared").linked_abs_id)
        self.assertEqual(len(set(owners)), 1, owners)

        resolved_b = self.service.resolve_download("abs-b")
        self.assertIsNone(resolved_b, "Second book must be unavailable due to hash conflict")

    def test_atomic_concurrent_adoption_refusal(self):
        """Interleaving regression: a foreign ensure_linked inside adopt must not allow A to serve."""
        self._write_book_file("shared.epub")
        self.db.save_book(Book(abs_id="abs-a", abs_title="Book A",
                               original_ebook_filename="shared.epub", status="active"))
        self.db.save_book(Book(abs_id="abs-b", abs_title="Book B",
                               original_ebook_filename="shared.epub", status="active"))

        original_adopt = self.service.database_service.adopt_kosync_document_if_unowned
        def adopt_with_interleaving(doc_hash, abs_id, expected_storyteller_uuid=None, owner_user_id=None):
            # Simulate another book linking the hash after pre-read but before atomic write
            if abs_id == "abs-a":
                self.db.ensure_linked_kosync_document(doc_hash, "abs-b")
            return original_adopt(doc_hash, abs_id, expected_storyteller_uuid=expected_storyteller_uuid, owner_user_id=owner_user_id)

        with patch.object(self.service.database_service, "adopt_kosync_document_if_unowned", side_effect=adopt_with_interleaving):
            resolved = self.service._resolve_download_artifact(
                self.db.get_book("abs-a"), link_hashes=True, allow_revalidation=False
            )
        # Artifact must be unavailable because adopt verification sees owner B
        self.assertIsNone(resolved)
        self.assertEqual(self.db.get_kosync_document("hash-shared").linked_abs_id, "abs-b")

    def test_scoped_user_cannot_adopt_unlinked_document_owned_by_other_user(self):
        """A scoped service must not adopt a document that belongs to another user."""
        self._write_book_file("shared.epub")
        user_a = self.db.create_user("userC", password="p")
        user_b = self.db.create_user("userD", password="p")
        self.db.save_book(Book(abs_id="abs-a", abs_title="Book A",
                               original_ebook_filename="shared.epub", status="active", user_id=user_a.id))
        self.db.save_book(Book(abs_id="abs-b", abs_title="Book B",
                               original_ebook_filename="shared.epub", status="active", user_id=user_b.id))
        # Create an unlinked document owned by user A
        from src.db.models import KosyncDocument
        doc = KosyncDocument(document_hash="hash-shared", user_id=user_a.id)
        self.db.save_kosync_document(doc)
        self.service.user_id = user_b.id
        resolved = self.service._resolve_download_artifact(
            self.db.get_book("abs-b"), link_hashes=True, allow_revalidation=False
        )
        # Adopt should be refused because document is owned by user A
        self.assertIsNone(resolved)
        # Document remains unlinked to abs-b
        doc_after = self.db.get_kosync_document("hash-shared")
        self.assertEqual(doc_after.linked_abs_id, None)

    def test_primary_hash_reserved_by_another_book_without_document_row(self):
        """If another book has kosync_doc_id set to the hash but no KosyncDocument row exists,
        adopt must refuse and artifact must be unavailable."""
        self._write_book_file("shared.epub")
        self.db.save_book(Book(abs_id="abs-a", abs_title="Book A",
                               original_ebook_filename="shared.epub", status="active",
                               kosync_doc_id="hash-shared"))
        self.db.save_book(Book(abs_id="abs-b", abs_title="Book B",
                               original_ebook_filename="shared.epub", status="active"))
        # No KosyncDocument row for hash-shared
        resolved = self.service._resolve_download_artifact(
            self.db.get_book("abs-b"), link_hashes=True, allow_revalidation=False
        )
        self.assertIsNone(resolved)

    def test_filename_mode_shared_content_hash_does_not_block_unique_filename_hash(self):
        """In filename mode the content hash may be shared, but each book has a unique
        filename hash. A content-hash conflict must not prevent the unique filename-hash
        artifact from being served."""
        self._use_filename_hashing()
        self._write_book_file("shared.epub")
        self.db.save_book(Book(abs_id="abs-a", abs_title="Book A",
                               original_ebook_filename="shared.epub", status="active"))
        self.db.save_book(Book(abs_id="abs-b", abs_title="Book B",
                               original_ebook_filename="shared.epub", status="active"))

        manifest = self.service.build_manifest()
        # Both books should appear because their filename hashes are unique
        abs_ids = {item["abs_id"] for item in manifest["books"]}
        self.assertEqual(abs_ids, {"abs-a", "abs-b"})
        # Content hash is shared, so it should stay with the first owner
        content_owner = self.db.get_kosync_document("content-shared").linked_abs_id
        self.assertIn(content_owner, ("abs-a", "abs-b"))
        # Filename hashes must be linked correctly
        self.assertIsNotNone(self.db.get_kosync_document("name-Book A.epub"))
        self.assertIsNotNone(self.db.get_kosync_document("name-Book B.epub"))

