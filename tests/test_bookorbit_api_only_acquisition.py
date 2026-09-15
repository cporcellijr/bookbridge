"""Regression tests for BookOrbit API-only EPUB acquisition (issue #352, v7.3.2).

Root cause: when a user's ebooks live only in BookOrbit (no /books bind mount),
suggestions matched a book and stored a correct mapping
(ebook_source='BookOrbit', ebook_source_id='47',
ebook_filename='07. Agent in Place (2018).epub'), but background processing
failed because _resolve_local_epub_uncached only ever received the *filename*
and searched BookOrbit's title-metadata index for the stem; the stored id was
never used. "07. Agent in Place (2018)" does not match the title "Agent in Place".

The fix adds _download_epub_by_source_id which looks up the mapping row by
ebook_filename and downloads by the stored source_id from the appropriate
client (BookOrbit or BookLore), running before the fragile filename-search
branches.
"""

import tempfile
import shutil
import unittest
import os
from pathlib import Path
from unittest.mock import MagicMock, call, patch

from src.db.models import Book
from src.sync_manager import SyncManager
from src.services.library_service import LibraryService
from src.api.bookorbit_client import BookOrbitClient, _MAX_FILENAME_QUERIES
from src.utils.cache_paths import safe_cache_path


def _build_manager(tmp_path):
    """Mirror the pattern from test_sync_manager_epub_hydration.py."""
    db = MagicMock()
    db.get_books_by_status.return_value = []
    manager = SyncManager(
        abs_client=MagicMock(),
        booklore_client=MagicMock(),
        bookorbit_client=MagicMock(),
        hardcover_client=MagicMock(),
        transcriber=MagicMock(),
        ebook_parser=MagicMock(),
        database_service=db,
        storyteller_client=MagicMock(),
        sync_clients={},
        alignment_service=None,
        library_service=None,
        migration_service=None,
        epub_cache_dir=tmp_path / "epub_cache",
        data_dir=tmp_path,
        books_dir=tmp_path / "books",
    )
    return manager


def _make_book_row(ebook_filename, ebook_source, ebook_source_id, abs_id="test-abs-id"):
    """Create a mock Book row as returned by get_book_by_ebook_filename."""
    book = MagicMock(spec=Book)
    book.abs_id = abs_id
    book.ebook_filename = ebook_filename
    book.ebook_source = ebook_source
    book.ebook_source_id = ebook_source_id
    book.original_ebook_filename = None
    return book


class TestBookOrbitApiOnlyAcquisition(unittest.TestCase):
    """Tests for _download_epub_by_source_id and its integration in _resolve_local_epub_uncached."""

    def setUp(self):
        self._temp_dir = tempfile.mkdtemp()
        self.tmp_path = Path(self._temp_dir)

    def tearDown(self):
        shutil.rmtree(self._temp_dir, ignore_errors=True)

    def _setup_manager_for_resolution(self, manager):
        """Configure manager so resolution reaches _download_epub_by_source_id."""
        # Bypass parser resolver (step 1) — it would short-circuit with a truthy MagicMock.
        manager.ebook_parser.resolve_book_path.side_effect = FileNotFoundError("No such file")
        # Ensure books_dir exists but is empty (step 2 finds nothing).
        manager.books_dir.mkdir(parents=True, exist_ok=True)
        # epub_cache_dir will be created by the method under test.
        return manager

    def test_core_regression_by_id_download_bookorbit(self):
        """
        A — Core regression: by-id download from BookOrbit.

        Mapping row has ebook_source='BookOrbit', ebook_source_id='47',
        ebook_filename='07. Agent in Place (2018).epub'. BookOrbit client
        is_configured() True, download_book('47') returns real bytes. Empty
        books dir, empty epub cache. Assert: returns the cache path, the bytes
        are on disk, download_book called once with '47', and find_book_by_filename
        was NEVER called.
        """
        manager = _build_manager(self.tmp_path)
        manager = self._setup_manager_for_resolution(manager)

        ebook_filename = "07. Agent in Place (2018).epub"
        book_row = _make_book_row(ebook_filename, "BookOrbit", "47")
        manager.database_service.get_book_by_ebook_filename.return_value = book_row

        bookorbit_client = manager.bookorbit_client
        bookorbit_client.is_configured.return_value = True
        bookorbit_client.download_book.return_value = b"EPUB content bytes"
        # Track calls to the filename-search branch.
        bookorbit_client.find_book_by_filename = MagicMock(return_value=None)

        # BookLore client should not be touched.
        booklore_client = manager.booklore_client
        booklore_client.is_configured.return_value = True
        booklore_client.find_book_by_filename = MagicMock(return_value=None)

        cached_path = manager.epub_cache_dir / ebook_filename
        result = manager._resolve_local_epub_uncached(ebook_filename)

        # Returns the cache path.
        self.assertEqual(result, cached_path)
        # File written to cache.
        self.assertTrue(cached_path.exists())
        self.assertEqual(cached_path.read_bytes(), b"EPUB content bytes")
        # By-id download called with the stored source_id.
        bookorbit_client.download_book.assert_called_once_with("47")
        # Filename-search branch NOT called — the whole point of the fix.
        bookorbit_client.find_book_by_filename.assert_not_called()
        # BookLore untouched.
        booklore_client.find_book_by_filename.assert_not_called()

    def test_grimmory_uses_same_path_booklore(self):
        """
        B — Grimmory uses the same path.

        As A but ebook_source='BookLore'; assert the BookLore client downloaded
        and the BookOrbit client was untouched.
        """
        manager = _build_manager(self.tmp_path)
        manager = self._setup_manager_for_resolution(manager)

        ebook_filename = "Agent in Place.epub"
        book_row = _make_book_row(ebook_filename, "BookLore", "grimmory-123")
        manager.database_service.get_book_by_ebook_filename.return_value = book_row

        booklore_client = manager.booklore_client
        booklore_client.is_configured.return_value = True
        booklore_client.download_book.return_value = b"Grimmory EPUB bytes"
        booklore_client.find_book_by_filename = MagicMock(return_value=None)

        bookorbit_client = manager.bookorbit_client
        bookorbit_client.is_configured.return_value = True
        bookorbit_client.find_book_by_filename = MagicMock(return_value=None)

        cached_path = manager.epub_cache_dir / ebook_filename
        result = manager._resolve_local_epub_uncached(ebook_filename)

        self.assertEqual(result, cached_path)
        self.assertTrue(cached_path.exists())
        self.assertEqual(cached_path.read_bytes(), b"Grimmory EPUB bytes")
        booklore_client.download_book.assert_called_once_with("grimmory-123")
        booklore_client.find_book_by_filename.assert_not_called()
        bookorbit_client.find_book_by_filename.assert_not_called()
        bookorbit_client.download_book.assert_not_called()

    def test_grimmory_source_spelling_variants_all_download_by_id(self):
        for index, source in enumerate(("grimmory", "Booklore", "BookLore")):
            with self.subTest(source=source):
                manager = self._setup_manager_for_resolution(_build_manager(self.tmp_path))
                ebook_filename = f"variant-{index}.epub"
                manager.database_service.get_book_by_ebook_filename.return_value = (
                    _make_book_row(ebook_filename, source, "4")
                )
                manager.booklore_client.is_configured.return_value = True
                manager.booklore_client.download_book.return_value = b"Grimmory bytes"
                manager.booklore_client.find_book_by_filename = MagicMock(return_value=None)

                result = manager._resolve_local_epub_uncached(ebook_filename)

                self.assertEqual(result, manager.epub_cache_dir / ebook_filename)
                manager.booklore_client.download_book.assert_called_once_with("4")
                manager.booklore_client.find_book_by_filename.assert_not_called()

    def test_reconciled_remote_name_reuses_original_cached_file(self):
        manager = self._setup_manager_for_resolution(_build_manager(self.tmp_path))
        old_filename = "old-name.epub"
        new_filename = "new-name.epub"
        book_row = _make_book_row(new_filename, "Booklore", "4")
        book_row.original_ebook_filename = old_filename
        manager.database_service.get_book_by_ebook_filename.return_value = book_row
        manager.epub_cache_dir.mkdir(parents=True, exist_ok=True)
        old_path = manager.epub_cache_dir / old_filename
        old_path.write_bytes(b"existing cached bytes")

        result = manager._resolve_local_epub_uncached(new_filename)

        self.assertEqual(result, old_path)
        self.assertFalse((manager.epub_cache_dir / new_filename).exists())
        manager.booklore_client.download_book.assert_not_called()

    def test_legacy_mapping_falls_back_to_filename_search(self):
        """
        C — Legacy mapping falls back.

        Row present, ebook_source_id None. Assert no by-id download_book, and
        that the pre-existing BookOrbit find_book_by_filename branch still RAN
        (assert it was called) — legacy rows keep their old behaviour.
        """
        manager = _build_manager(self.tmp_path)
        manager = self._setup_manager_for_resolution(manager)

        ebook_filename = "Legacy Book.epub"
        book_row = _make_book_row(ebook_filename, "BookOrbit", None)  # No source_id
        manager.database_service.get_book_by_ebook_filename.return_value = book_row

        bookorbit_client = manager.bookorbit_client
        bookorbit_client.is_configured.return_value = True
        bookorbit_client.download_book = MagicMock(return_value=b"legacy epub bytes")
        # Return a fake book from filename search so the legacy branch completes.
        bookorbit_client.find_book_by_filename.return_value = {"id": "found-by-filename"}

        # Grimmory runs first in the filename-search chain; make it miss.
        manager.booklore_client.is_configured.return_value = True
        manager.booklore_client.find_book_by_filename = MagicMock(return_value=None)

        result = manager._resolve_local_epub_uncached(ebook_filename)

        # Filename-search branch WAS called (legacy behaviour preserved).
        bookorbit_client.find_book_by_filename.assert_called_once_with(ebook_filename)
        # The ONLY download was the legacy one, keyed by the id the filename search
        # found. The new by-id step never fired, because the row has no source id.
        self.assertEqual(
            bookorbit_client.download_book.call_args_list,
            [call("found-by-filename")],
        )
        self.assertEqual(result, manager.epub_cache_dir / ebook_filename)

    def test_storyteller_artifacts_protected(self):
        """
        D — Storyteller artifacts protected.

        _resolve_local_epub_uncached('storyteller_abcd.epub') with a mapping
        whose ebook_source_id is set must NOT call download_book; caching
        library bytes under the artifact name would corrupt a tri-link.
        """
        manager = _build_manager(self.tmp_path)
        manager = self._setup_manager_for_resolution(manager)

        ebook_filename = "storyteller_abcd1234.epub"
        # Mapping has source_id but filename starts with storyteller_
        book_row = _make_book_row(ebook_filename, "BookOrbit", "47")
        manager.database_service.get_book_by_ebook_filename.return_value = book_row

        bookorbit_client = manager.bookorbit_client
        bookorbit_client.is_configured.return_value = True
        bookorbit_client.download_book = MagicMock()
        bookorbit_client.find_book_by_filename = MagicMock(return_value=None)

        booklore_client = manager.booklore_client
        booklore_client.is_configured.return_value = True
        booklore_client.find_book_by_filename = MagicMock(return_value=None)

        result = manager._resolve_local_epub_uncached(ebook_filename)

        # Returns None (falls through, no by-id download, no filename search for storyteller_)
        self.assertIsNone(result)
        # By-id download NOT called for storyteller_ filenames.
        bookorbit_client.download_book.assert_not_called()
        booklore_client.download_book.assert_not_called()
        # The pre-existing filename-search branches still run for storyteller_ names
        # (unchanged behaviour); they simply find nothing here. Only the by-id step
        # is guarded, which is what protects the artifact from being overwritten.

    def test_failure_falls_through_without_raising_download_returns_none(self):
        """
        E1 — Failure falls through without raising: download_book returns None.

        By-id download returns None; must return normally (not propagate) and
        still let the filename-search branch run.
        """
        manager = _build_manager(self.tmp_path)
        manager = self._setup_manager_for_resolution(manager)

        ebook_filename = "Fail Download.epub"
        book_row = _make_book_row(ebook_filename, "BookOrbit", "47")
        manager.database_service.get_book_by_ebook_filename.return_value = book_row

        bookorbit_client = manager.bookorbit_client
        bookorbit_client.is_configured.return_value = True
        # By-id attempt returns nothing; the later fallback download succeeds.
        bookorbit_client.download_book.side_effect = [None, b"fallback epub bytes"]
        bookorbit_client.find_book_by_filename = MagicMock(return_value={"id": "fallback-id"})

        booklore_client = manager.booklore_client
        booklore_client.is_configured.return_value = True
        booklore_client.find_book_by_filename = MagicMock(return_value=None)

        # Should not raise, should fall through to filename search.
        cached_path = manager.epub_cache_dir / ebook_filename
        result = manager._resolve_local_epub_uncached(ebook_filename)

        # The by-id attempt came FIRST and was not fatal; the fallback then ran.
        self.assertEqual(bookorbit_client.download_book.call_args_list[0], call("47"))
        # Filename search branch ran.
        bookorbit_client.find_book_by_filename.assert_called_once_with(ebook_filename)
        # And it downloaded the fallback.
        self.assertEqual(bookorbit_client.download_book.call_count, 2)
        bookorbit_client.download_book.assert_called_with("fallback-id")
        # Result is the cached file from fallback download.
        self.assertEqual(result, cached_path)
        self.assertTrue(cached_path.exists())

    def test_failure_falls_through_without_raising_download_raises(self):
        """
        E2 — Failure falls through without raising: download_book raises.

        By-id download raises exception; must return normally (not propagate)
        and still let the filename-search branch run.
        """
        manager = _build_manager(self.tmp_path)
        manager = self._setup_manager_for_resolution(manager)

        ebook_filename = "Fail Download Raise.epub"
        book_row = _make_book_row(ebook_filename, "BookOrbit", "47")
        manager.database_service.get_book_by_ebook_filename.return_value = book_row

        bookorbit_client = manager.bookorbit_client
        bookorbit_client.is_configured.return_value = True
        # By-id attempt raises; the later fallback download still succeeds.
        bookorbit_client.download_book.side_effect = [
            Exception("Network error"),
            b"fallback epub bytes",
        ]
        bookorbit_client.find_book_by_filename = MagicMock(return_value={"id": "fallback-id"})

        booklore_client = manager.booklore_client
        booklore_client.is_configured.return_value = True
        booklore_client.find_book_by_filename = MagicMock(return_value=None)

        # Should not raise, should fall through to filename search.
        cached_path = manager.epub_cache_dir / ebook_filename
        result = manager._resolve_local_epub_uncached(ebook_filename)

        # The by-id attempt came FIRST and was not fatal; the fallback then ran.
        self.assertEqual(bookorbit_client.download_book.call_args_list[0], call("47"))
        # Filename search branch ran.
        bookorbit_client.find_book_by_filename.assert_called_once_with(ebook_filename)
        # And it downloaded the fallback.
        self.assertEqual(bookorbit_client.download_book.call_count, 2)
        bookorbit_client.download_book.assert_called_with("fallback-id")
        # Result is the cached file from fallback download.
        self.assertEqual(result, cached_path)
        self.assertTrue(cached_path.exists())

    def test_local_cache_still_wins(self):
        """
        F — Local cache still wins.

        Pre-create the file in the epub cache dir. Assert the cache path is
        returned and download_book was never called — no new network round trip
        for an already-local book.
        """
        manager = _build_manager(self.tmp_path)
        manager = self._setup_manager_for_resolution(manager)

        ebook_filename = "Cached Book.epub"
        book_row = _make_book_row(ebook_filename, "BookOrbit", "47")
        manager.database_service.get_book_by_ebook_filename.return_value = book_row

        # Pre-populate the cache.
        manager.epub_cache_dir.mkdir(parents=True, exist_ok=True)
        cached_path = manager.epub_cache_dir / ebook_filename
        cached_path.write_bytes(b"Already cached content")

        bookorbit_client = manager.bookorbit_client
        bookorbit_client.is_configured.return_value = True
        bookorbit_client.download_book = MagicMock()
        bookorbit_client.find_book_by_filename = MagicMock(return_value=None)

        booklore_client = manager.booklore_client
        booklore_client.is_configured.return_value = True
        booklore_client.find_book_by_filename = MagicMock(return_value=None)

        result = manager._resolve_local_epub_uncached(ebook_filename)

        # Returns the cache path immediately.
        self.assertEqual(result, cached_path)
        self.assertEqual(cached_path.read_bytes(), b"Already cached content")
        # No network calls at all.
        bookorbit_client.download_book.assert_not_called()
        bookorbit_client.find_book_by_filename.assert_not_called()
        booklore_client.find_book_by_filename.assert_not_called()


class TestLibraryServicePriority0(unittest.TestCase):
    """Tests for LibraryService.acquire_ebook Priority 0 — Explicit mapping."""

    def setUp(self):
        self._temp_dir = tempfile.mkdtemp()
        self.tmp_path = Path(self._temp_dir)
        self.epub_cache_dir = str(self.tmp_path / "epub_cache")

        self.mock_db = MagicMock()
        self.mock_booklore = MagicMock()
        self.mock_cwa = MagicMock()
        self.mock_abs = MagicMock()
        self.mock_bookorbit = MagicMock()

        self.service = LibraryService(
            database_service=self.mock_db,
            booklore_client=self.mock_booklore,
            cwa_client=self.mock_cwa,
            abs_client=self.mock_abs,
            epub_cache_dir=self.epub_cache_dir,
            bookorbit_client=self.mock_bookorbit,
        )

    def tearDown(self):
        shutil.rmtree(self._temp_dir, ignore_errors=True)

    def _make_book_mock(self, ebook_filename, ebook_source, ebook_source_id, abs_id="test-abs-id"):
        """Create a mock Book row with the required attributes."""
        book = MagicMock(spec=Book)
        book.abs_id = abs_id
        book.ebook_filename = ebook_filename
        book.ebook_source = ebook_source
        book.ebook_source_id = ebook_source_id
        book.original_ebook_filename = None
        return book

    def _make_abs_item(self, item_id="item1", title="Test Book", author="Test Author"):
        """Create a normal-looking ABS item dict."""
        return {
            "id": item_id,
            "media": {
                "metadata": {
                    "title": title,
                    "authorName": author,
                }
            }
        }

    def test_explicit_bookorbit_mapping_short_circuits_chain(self):
        """
        Explicit BookOrbit mapping short-circuits the chain.

        book has ebook_source='BookOrbit', ebook_source_id='47',
        ebook_filename='07. Agent in Place (2018).epub'; bookorbit_client.is_configured()
        True and download_book('47') returns >1024 bytes. Call acquire_ebook(abs_item, book)
        with a normal-looking abs_item. Assert: the returned path is inside the cache dir
        and named after book.ebook_filename, the bytes are on disk, download_book was called
        with '47', and — the point of the follow-up — abs_client.get_ebook_files and
        abs_client.search_ebooks were NEVER called.
        """
        ebook_filename = "07. Agent in Place (2018).epub"
        book = self._make_book_mock(ebook_filename, "BookOrbit", "47")
        abs_item = self._make_abs_item()

        self.mock_bookorbit.is_configured.return_value = True
        download_content = b"x" * 2048
        self.mock_bookorbit.download_book.return_value = download_content

        # Track calls to the later priorities
        self.mock_abs.get_ebook_files = MagicMock(return_value=[])
        self.mock_abs.search_ebooks = MagicMock(return_value=[])
        self.mock_cwa.is_configured.return_value = True
        self.mock_cwa.search_ebooks = MagicMock(return_value=[])

        result = self.service.acquire_ebook(abs_item, book)

        # Returns the cache path
        expected_path = safe_cache_path(self.epub_cache_dir, ebook_filename)
        self.assertEqual(result, str(expected_path))
        # File written to cache
        self.assertTrue(expected_path.exists())
        self.assertEqual(expected_path.read_bytes(), download_content)
        # By-id download called with the stored source_id
        self.mock_bookorbit.download_book.assert_called_once_with("47")
        # Later priorities NOT called
        self.mock_abs.get_ebook_files.assert_not_called()
        self.mock_abs.search_ebooks.assert_not_called()
        self.mock_cwa.search_ebooks.assert_not_called()

    def test_grimmory_mapping_uses_booklore(self):
        """
        Grimmory mapping uses self.booklore.

        Same as above but ebook_source='BookLore'; assert the booklore client downloaded
        and bookorbit_client.download_book was not called.
        """
        ebook_filename = "Agent in Place.epub"
        book = self._make_book_mock(ebook_filename, "BookLore", "grimmory-123")
        abs_item = self._make_abs_item()

        self.mock_booklore.is_configured.return_value = True
        download_content = b"y" * 2048
        self.mock_booklore.download_book.return_value = download_content

        self.mock_bookorbit.is_configured.return_value = True
        self.mock_bookorbit.download_book = MagicMock()

        self.mock_abs.get_ebook_files = MagicMock(return_value=[])
        self.mock_abs.search_ebooks = MagicMock(return_value=[])
        self.mock_cwa.is_configured.return_value = True
        self.mock_cwa.search_ebooks = MagicMock(return_value=[])

        result = self.service.acquire_ebook(abs_item, book)

        expected_path = safe_cache_path(self.epub_cache_dir, ebook_filename)
        self.assertEqual(result, str(expected_path))
        self.assertTrue(expected_path.exists())
        self.assertEqual(expected_path.read_bytes(), download_content)
        self.mock_booklore.download_book.assert_called_once_with("grimmory-123")
        self.mock_bookorbit.download_book.assert_not_called()
        self.mock_abs.get_ebook_files.assert_not_called()
        self.mock_abs.search_ebooks.assert_not_called()
        self.mock_cwa.search_ebooks.assert_not_called()

    def test_already_cached_file_is_reused(self):
        """
        Already-cached file is reused.

        Pre-write >1024 bytes at the expected cache path. Assert the path is returned
        and download_book was NOT called.
        """
        ebook_filename = "Cached Book.epub"
        book = self._make_book_mock(ebook_filename, "BookOrbit", "47")
        abs_item = self._make_abs_item()

        expected_path = safe_cache_path(self.epub_cache_dir, ebook_filename)
        expected_path.parent.mkdir(parents=True, exist_ok=True)
        expected_path.write_bytes(b"z" * 2048)

        self.mock_bookorbit.is_configured.return_value = True
        self.mock_bookorbit.download_book = MagicMock()

        self.mock_abs.get_ebook_files = MagicMock(return_value=[])
        self.mock_abs.search_ebooks = MagicMock(return_value=[])
        self.mock_cwa.is_configured.return_value = True
        self.mock_cwa.search_ebooks = MagicMock(return_value=[])

        result = self.service.acquire_ebook(abs_item, book)

        self.assertEqual(result, str(expected_path))
        self.assertEqual(expected_path.read_bytes(), b"z" * 2048)
        self.mock_bookorbit.download_book.assert_not_called()

    def test_book_none_leaves_old_chain_untouched(self):
        """
        book=None leaves the old chain untouched.

        Call acquire_ebook(abs_item) with no book and an ABS item that has an ebook file;
        assert Priority 1 behaves exactly as tests/test_library_priority.py expects
        (returns the <item_id>_direct.epub cache path) and that no BookOrbit call happened.
        """
        abs_item = self._make_abs_item(item_id="item1")

        self.mock_abs.get_ebook_files.return_value = [{"stream_url": "url", "ext": "epub"}]
        self.mock_abs.download_file.return_value = True

        self.mock_bookorbit.is_configured.return_value = True
        self.mock_bookorbit.download_book = MagicMock()

        self.mock_cwa.is_configured.return_value = True
        self.mock_cwa.search_ebooks = MagicMock(return_value=[])

        result = self.service.acquire_ebook(abs_item, book=None)

        expected = os.path.join(self.epub_cache_dir, "item1_direct.epub")
        self.assertEqual(result, expected)
        self.mock_abs.get_ebook_files.assert_called_once_with("item1")
        self.mock_bookorbit.download_book.assert_not_called()

    def test_storyteller_artifacts_skipped(self):
        """
        Storyteller artifacts are skipped.

        book.ebook_filename = 'storyteller_abcd.epub' with a BookOrbit source id set;
        assert download_book was not called and the chain fell through to the later priorities.
        """
        ebook_filename = "storyteller_abcd1234.epub"
        book = self._make_book_mock(ebook_filename, "BookOrbit", "47")
        abs_item = self._make_abs_item()

        self.mock_bookorbit.is_configured.return_value = True
        self.mock_bookorbit.download_book = MagicMock()

        self.mock_abs.get_ebook_files = MagicMock(return_value=[])
        self.mock_abs.search_ebooks = MagicMock(return_value=[])
        self.mock_cwa.is_configured.return_value = True
        self.mock_cwa.search_ebooks = MagicMock(return_value=[])

        result = self.service.acquire_ebook(abs_item, book)

        # Priority 0 skipped, falls through; no ABS ebook files so returns None
        self.assertIsNone(result)
        self.mock_bookorbit.download_book.assert_not_called()
        # Later priorities still got their chance
        self.mock_abs.get_ebook_files.assert_called_once()

    def test_failed_download_falls_through_does_not_raise_returns_none(self):
        """
        A failed download falls through, does not raise: download_book returns None.

        download_book returns None; assert acquire_ebook returns normally and the later
        priorities still got their chance (e.g. abs_client.get_ebook_files WAS called).
        """
        ebook_filename = "Fail Download.epub"
        book = self._make_book_mock(ebook_filename, "BookOrbit", "47")
        abs_item = self._make_abs_item()

        self.mock_bookorbit.is_configured.return_value = True
        self.mock_bookorbit.download_book.return_value = None

        self.mock_abs.get_ebook_files = MagicMock(return_value=[])
        self.mock_abs.search_ebooks = MagicMock(return_value=[])
        self.mock_cwa.is_configured.return_value = True
        self.mock_cwa.search_ebooks = MagicMock(return_value=[])

        result = self.service.acquire_ebook(abs_item, book)

        # Returns None (all priorities exhausted)
        self.assertIsNone(result)
        self.mock_bookorbit.download_book.assert_called_once_with("47")
        # Later priorities still ran
        self.mock_abs.get_ebook_files.assert_called_once()

    def test_failed_download_falls_through_does_not_raise_raises_exception(self):
        """
        A failed download falls through, does not raise: download_book raises.

        download_book raises exception; assert acquire_ebook returns normally and the later
        priorities still got their chance.
        """
        ebook_filename = "Fail Download Raise.epub"
        book = self._make_book_mock(ebook_filename, "BookOrbit", "47")
        abs_item = self._make_abs_item()

        self.mock_bookorbit.is_configured.return_value = True
        self.mock_bookorbit.download_book.side_effect = Exception("Network error")

        self.mock_abs.get_ebook_files = MagicMock(return_value=[])
        self.mock_abs.search_ebooks = MagicMock(return_value=[])
        self.mock_cwa.is_configured.return_value = True
        self.mock_cwa.search_ebooks = MagicMock(return_value=[])

        result = self.service.acquire_ebook(abs_item, book)

        # Returns None (all priorities exhausted)
        self.assertIsNone(result)
        self.mock_bookorbit.download_book.assert_called_once_with("47")
        # Later priorities still ran
        self.mock_abs.get_ebook_files.assert_called_once()


class TestBookOrbitClientFindBookByFilenameQueries(unittest.TestCase):
    """Tests for BookOrbitClient.find_book_by_filename query variants."""

    def setUp(self):
        with patch.dict(os.environ, {
            "BOOKORBIT_SERVER": "http://mock",
            "BOOKORBIT_USER": "u",
            "BOOKORBIT_PASSWORD": "p",
        }):
            self.client = BookOrbitClient()
        # Ensure clean state
        self.client._filename_index = {}
        self.client._book_cache = {}
        self.client._detail_cache = {}
        self.client._llm_match_cache = {}

    def _stub_search_and_detail(self, search_returns, detail_returns):
        """Stub _search_raw to record queries and return search_returns; stub get_book_detail."""
        self.recorded_queries = []

        def fake_search_raw(query, limit=20):
            self.recorded_queries.append(query)
            return search_returns

        def fake_get_book_detail(book_id, force=False):
            return detail_returns.get(book_id)

        self.client._search_raw = MagicMock(side_effect=fake_search_raw)
        self.client.get_book_detail = MagicMock(side_effect=fake_get_book_detail)
        self.client._llm_match_from_cache = MagicMock(return_value=None)

    def test_reporter_filename_produces_usable_query(self):
        """
        The reporter's filename now produces a usable query.

        Call find_book_by_filename('07. Agent in Place (2018).epub') and assert the
        recorded queries include 'Agent in Place' — proving the '07. ' prefix and the
        ' (2018)' suffix are both stripped. Assert the raw stem is still the FIRST
        query tried (exact matches must keep priority).
        """
        self._stub_search_and_detail([], {})

        result = self.client.find_book_by_filename("07. Agent in Place (2018).epub")

        self.assertIsNone(result)
        # Raw stem is first
        self.assertEqual(self.recorded_queries[0], "07. Agent in Place (2018)")
        # Stripped variants present
        self.assertIn("Agent in Place", self.recorded_queries)
        # Both prefix and suffix stripped variant
        self.assertIn("Agent in Place", self.recorded_queries)

    def test_real_world_filename_with_author_tail_produces_title_query(self):
        """
        Real-world filename with " - Author (Year)" tail produces the bare title query.

        The defect: '07. Naomi's Room - Jonathan Aycliffe (2018).epub' must issue the
        query "Naomi's Room" (series prefix AND author tail AND year all stripped).
        This variant was previously generated at position 6 and cut by the cap of 5.
        Assert that exact query string is in the recorded queries.
        Also assert raw stem is still first and total queries <= _MAX_FILENAME_QUERIES.
        """
        self._stub_search_and_detail([], {})

        filename = "07. Naomi's Room - Jonathan Aycliffe (2018).epub"
        result = self.client.find_book_by_filename(filename)

        self.assertIsNone(result)
        # Raw stem is first
        self.assertEqual(self.recorded_queries[0], "07. Naomi's Room - Jonathan Aycliffe (2018)")
        # The critical variant that actually resolves
        self.assertIn("Naomi's Room", self.recorded_queries)
        # Cap respected
        self.assertLessEqual(len(self.recorded_queries), _MAX_FILENAME_QUERIES)

    def test_widened_queries_still_reject_wrong_book_with_author_tail(self):
        """
        Widened queries still reject a wrong book for the real-world shape.

        _search_raw returns a hit for one of the derived queries, but get_book_detail
        returns a detail whose files[].filename does not match — the method must still
        return None.
        """
        ebook_filename = "07. Naomi's Room - Jonathan Aycliffe (2018).epub"
        target_name = ebook_filename.lower()
        hit_id = 99
        search_returns = [{"id": hit_id, "title": "Naomi's Room", "formats": ["epub"]}]
        detail_returns = {
            hit_id: {
                "id": hit_id,
                "title": "Naomi's Room",
                "files": [{"filename": "Different Book.epub", "format": "epub", "role": "primary"}],
            }
        }
        self._stub_search_and_detail(search_returns, detail_returns)

        result = self.client.find_book_by_filename(ebook_filename)

        self.assertIsNone(result)

    def test_query_count_is_capped(self):
        """
        Query count is capped.

        With a filename engineered to produce many variants, assert
        len(recorded_queries) <= _MAX_FILENAME_QUERIES (import the constant, do not
        hard-code 5).
        """
        # Filename with many " - " segments and series prefix/year suffix
        filename = "01. Book One - Author - Subtitle - Extra (2020).epub"
        self._stub_search_and_detail([], {})

        self.client.find_book_by_filename(filename)

        self.assertLessEqual(len(self.recorded_queries), _MAX_FILENAME_QUERIES)

    def test_confirmation_not_weakened(self):
        """
        Confirmation was not weakened.

        _search_raw returns a hit, but get_book_detail returns a detail whose
        files[].filename does NOT match the requested filename. Assert the method
        returns None — a widened query must never let a wrong book through.
        """
        ebook_filename = "07. Agent in Place (2018).epub"
        target_name = ebook_filename.lower()
        hit_id = 42
        search_returns = [{"id": hit_id, "title": "Wrong Book", "formats": ["epub"]}]
        detail_returns = {
            hit_id: {
                "id": hit_id,
                "title": "Wrong Book",
                "files": [{"filename": "Different Book.epub", "format": "epub", "role": "primary"}],
            }
        }
        self._stub_search_and_detail(search_returns, detail_returns)

        result = self.client.find_book_by_filename(ebook_filename)

        self.assertIsNone(result)

    def test_matching_filename_is_still_accepted(self):
        """
        A matching filename is still accepted.

        Same setup but the detail's files[].filename DOES match; assert the method
        returns the hit's id.
        """
        ebook_filename = "07. Agent in Place (2018).epub"
        target_name = ebook_filename.lower()
        hit_id = 42
        search_returns = [{"id": hit_id, "title": "Agent in Place", "formats": ["epub"]}]
        detail_returns = {
            hit_id: {
                "id": hit_id,
                "title": "Agent in Place",
                "files": [{"filename": ebook_filename, "format": "epub", "role": "primary"}],
            }
        }
        self._stub_search_and_detail(search_returns, detail_returns)

        result = self.client.find_book_by_filename(ebook_filename)

        self.assertIsNotNone(result)
        self.assertEqual(result["id"], hit_id)


if __name__ == "__main__":
    unittest.main(verbosity=2)
