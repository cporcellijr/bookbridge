"""The filesystem hash scan reuses a cached hash when the file's mtime is unchanged.
After KOSYNC_HASH_METHOD changes, a hash cached under the old method must be
recomputed, or discovery keeps comparing device hashes against the old ones.
"""
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.api import kosync_server

NAME = "By Night in Chile - Roberto Bolaño.epub"
FILENAME_HASH = hashlib.md5(NAME.encode("utf-8")).hexdigest()
CONTENT_HASH = "37692aed0e34dd6477279ad11db2e32c"


class TestStaleFilesystemHashCache(unittest.TestCase):
    def setUp(self):
        self._saved = (kosync_server._database_service, kosync_server._container)
        self.addCleanup(self._restore)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.scan_dir = Path(self.tmp.name)
        self.epub = self.scan_dir / NAME
        self.epub.write_bytes(b"epub bytes")
        self.db = MagicMock()
        kosync_server._database_service = self.db
        self.parser = MagicMock()
        container = MagicMock()
        container.ebook_parser.return_value = self.parser
        kosync_server._container = container

    def _restore(self):
        kosync_server._database_service, kosync_server._container = self._saved

    def _cached(self, document_hash):
        self.db.get_kosync_doc_by_filename.return_value = SimpleNamespace(
            document_hash=document_hash, mtime=os.stat(self.epub).st_mtime,
        )

    def _mode(self, method):
        self.parser.hash_method = method
        self.parser.get_kosync_id.return_value = FILENAME_HASH if method == "filename" else CONTENT_HASH

    def _scan(self, doc_hash):
        with patch.object(kosync_server, "_cache_kosync_metadata") as cache:
            return kosync_server._scan_directory_for_hash(self.scan_dir, doc_hash), cache

    def test_filename_mode_recomputes_a_cached_content_hash(self):
        self._mode("filename")
        self._cached(CONTENT_HASH)

        result, cache = self._scan(FILENAME_HASH)

        self.assertEqual(result, NAME)
        self.parser.get_kosync_id.assert_called_once()
        self.assertEqual(cache.call_args.args[0], FILENAME_HASH)

    def test_filename_mode_reuses_a_cached_filename_hash(self):
        self._mode("filename")
        self._cached(FILENAME_HASH)

        result, _ = self._scan(FILENAME_HASH)

        self.assertEqual(result, NAME)
        self.parser.get_kosync_id.assert_not_called()

    def test_content_mode_recomputes_a_cached_filename_hash(self):
        self._mode("content")
        self._cached(FILENAME_HASH)

        result, _ = self._scan(CONTENT_HASH)

        self.assertEqual(result, NAME)
        self.parser.get_kosync_id.assert_called_once()

    def test_content_mode_reuses_a_cached_content_hash(self):
        self._mode("content")
        self._cached(CONTENT_HASH)

        result, _ = self._scan(CONTENT_HASH)

        self.assertEqual(result, NAME)
        self.parser.get_kosync_id.assert_not_called()


if __name__ == "__main__":
    unittest.main()
