"""Tests for BookOrbit-hosted hash discovery.

BookOrbit has no local files and no bulk-hash endpoint, so an unknown document
hash previously had no reactive path at all there — only Grimmory and the local
filesystem were searched. Matching costs one download per candidate, so the sweep
must prefer cached copies and stay bounded.
"""

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.api import kosync_server


def _book(abs_id, source, source_id, filename):
    return SimpleNamespace(
        abs_id=abs_id,
        abs_title=abs_id,
        ebook_source=source,
        ebook_source_id=source_id,
        ebook_filename=filename,
        original_ebook_filename=filename,
        status="active",
    )


class TestBookOrbitDiscovery(unittest.TestCase):
    KEY = "KOSYNC_BOOKORBIT_DISCOVERY_LIMIT"

    def setUp(self):
        self._saved = {
            "_container": kosync_server._container,
            "_database_service": kosync_server._database_service,
        }
        self._saved_env = os.environ.get(self.KEY)

        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name)

        self.bookorbit = MagicMock()
        self.bookorbit.is_configured.return_value = True
        self.bookorbit.get_all_books.return_value = []
        self.parser = MagicMock()

        container = MagicMock()
        container.bookorbit_client.return_value = self.bookorbit
        container.ebook_parser.return_value = self.parser
        container.data_dir.return_value = self.data_dir
        kosync_server._container = container

        self.db = MagicMock()
        kosync_server._database_service = self.db

    def tearDown(self):
        kosync_server._container = self._saved["_container"]
        kosync_server._database_service = self._saved["_database_service"]
        if self._saved_env is None:
            os.environ.pop(self.KEY, None)
        else:
            os.environ[self.KEY] = self._saved_env

    def test_limit_default_and_parsing(self):
        os.environ.pop(self.KEY, None)
        self.assertEqual(kosync_server._bookorbit_discovery_limit(), 40)
        os.environ[self.KEY] = "7"
        self.assertEqual(kosync_server._bookorbit_discovery_limit(), 7)
        os.environ[self.KEY] = "garbage"
        self.assertEqual(kosync_server._bookorbit_discovery_limit(), 40)
        os.environ[self.KEY] = "-3"
        self.assertEqual(kosync_server._bookorbit_discovery_limit(), 0)

    def test_finds_match_by_downloading(self):
        self.db.get_books_by_status.return_value = [
            _book("a", "bookorbit", "1", "a.epub"),
            _book("b", "bookorbit", "2", "b.epub"),
        ]
        self.bookorbit.download_book.side_effect = [b"AAA", b"BBB"]
        self.parser.get_kosync_id_from_bytes.side_effect = (
            lambda name, content: "want" if content == b"BBB" else "other"
        )

        result = kosync_server._scan_bookorbit_for_hash("want")

        self.assertEqual(result, "b.epub")
        self.assertTrue((self.data_dir / "epub_cache" / "b.epub").exists())

    def test_cached_copy_is_hashed_without_downloading(self):
        cache_dir = self.data_dir / "epub_cache"
        cache_dir.mkdir(parents=True)
        (cache_dir / "a.epub").write_bytes(b"CACHED")

        self.db.get_books_by_status.return_value = [_book("a", "bookorbit", "1", "a.epub")]
        self.parser.get_kosync_id.return_value = "want"

        result = kosync_server._scan_bookorbit_for_hash("want")

        self.assertEqual(result, "a.epub")
        self.bookorbit.download_book.assert_not_called()

    def test_download_budget_is_respected(self):
        os.environ[self.KEY] = "2"
        self.db.get_books_by_status.return_value = [
            _book(str(i), "bookorbit", str(i), f"{i}.epub") for i in range(10)
        ]
        self.bookorbit.download_book.return_value = b"DATA"
        self.parser.get_kosync_id_from_bytes.return_value = "nope"

        result = kosync_server._scan_bookorbit_for_hash("want")

        self.assertIsNone(result)
        self.assertEqual(self.bookorbit.download_book.call_count, 2)

    def test_zero_budget_disables_the_search(self):
        os.environ[self.KEY] = "0"
        self.db.get_books_by_status.return_value = [_book("a", "bookorbit", "1", "a.epub")]

        self.assertIsNone(kosync_server._scan_bookorbit_for_hash("want"))
        self.bookorbit.download_book.assert_not_called()

    def test_non_bookorbit_books_are_ignored(self):
        self.db.get_books_by_status.return_value = [
            _book("a", "booklore", "1", "a.epub"),
            _book("b", "ABS", "2", "b.epub"),
        ]

        self.assertIsNone(kosync_server._scan_bookorbit_for_hash("want"))
        self.bookorbit.download_book.assert_not_called()

    def test_unconfigured_client_is_a_no_op(self):
        self.bookorbit.is_configured.return_value = False
        self.assertIsNone(kosync_server._scan_bookorbit_for_hash("want"))
        self.db.get_books_by_status.assert_not_called()

    def test_download_failure_does_not_abort_the_sweep(self):
        self.db.get_books_by_status.return_value = [
            _book("a", "bookorbit", "1", "a.epub"),
            _book("b", "bookorbit", "2", "b.epub"),
        ]
        self.bookorbit.download_book.side_effect = [RuntimeError("boom"), b"BBB"]
        self.parser.get_kosync_id_from_bytes.return_value = "want"

        result = kosync_server._scan_bookorbit_for_hash("want")

        self.assertEqual(result, "b.epub")

    def test_unmatched_catalog_book_is_discovered(self):
        self.db.get_books_by_status.return_value = []
        self.bookorbit.get_all_books.return_value = [{"id": 42, "kind": "ebook"}]
        self.bookorbit._enrich_ebook.return_value = {"id": 42, "fileName": "new.epub"}
        self.bookorbit.download_book.return_value = b"NEW"
        self.parser.get_kosync_id_from_bytes.return_value = "want"

        self.assertEqual(kosync_server._scan_bookorbit_for_hash("want"), "new.epub")
        self.bookorbit.download_book.assert_called_once_with("42")

    def test_catalog_scan_keeps_budget_and_skips_tracked_ids_and_audio(self):
        os.environ[self.KEY] = "2"
        self.db.get_books_by_status.return_value = [_book("a", "bookorbit", "1", "a.epub")]
        self.bookorbit.get_all_books.return_value = [
            {"id": 1, "kind": "ebook"}, {"id": 2, "kind": "audiobook"},
            {"id": 3, "kinds": ["ebook", "audiobook"]}, {"id": 4, "kind": "ebook"},
        ]
        self.bookorbit._enrich_ebook.return_value = {"id": 3, "fileName": "3.epub"}
        self.bookorbit.download_book.return_value = b"MISS"
        self.parser.get_kosync_id_from_bytes.return_value = "other"

        self.assertIsNone(kosync_server._scan_bookorbit_for_hash("want"))
        self.assertEqual(self.bookorbit.download_book.call_count, 2)
        self.bookorbit._enrich_ebook.assert_called_once_with(3, {"id": 3, "kinds": ["ebook", "audiobook"]})

    def test_failed_downloads_also_spend_budget(self):
        os.environ[self.KEY] = "1"
        self.db.get_books_by_status.return_value = [
            _book("a", "bookorbit", "1", "a.epub"),
            _book("b", "bookorbit", "2", "b.epub"),
        ]
        self.bookorbit.download_book.side_effect = RuntimeError("offline")
        self.assertIsNone(kosync_server._scan_bookorbit_for_hash("want"))
        self.bookorbit.download_book.assert_called_once()


if __name__ == "__main__":
    unittest.main()
