"""Tests for cover staleness after an ebook is rewritten.

The extracted ebook cover is written once to /data/covers/<hash>.jpg and served
forever, and the dashboard prefers it over the live audio cover — so replacing a
cover inside an EPUB never showed up.
"""

import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from src.web_server import _extracted_cover_is_stale


class TestExtractedCoverFreshness(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cover = self.root / "cover.jpg"
        self.source = self.root / "book.epub"

    def _write(self, path: Path, mtime: float) -> None:
        path.write_bytes(b"x")
        os.utime(path, (mtime, mtime))

    def test_stale_when_ebook_is_newer_than_the_cover(self):
        now = time.time()
        self._write(self.cover, now - 3600)
        self._write(self.source, now)
        self.assertTrue(_extracted_cover_is_stale(self.cover, self.source))

    def test_fresh_when_cover_is_newer(self):
        now = time.time()
        self._write(self.source, now - 3600)
        self._write(self.cover, now)
        self.assertFalse(_extracted_cover_is_stale(self.cover, self.source))

    def test_missing_source_is_not_stale(self):
        self._write(self.cover, time.time())
        self.assertFalse(_extracted_cover_is_stale(self.cover, self.root / "gone.epub"))

    def test_none_source_is_not_stale(self):
        self._write(self.cover, time.time())
        self.assertFalse(_extracted_cover_is_stale(self.cover, None))

    def test_missing_cover_is_not_stale(self):
        self._write(self.source, time.time())
        self.assertFalse(_extracted_cover_is_stale(self.root / "gone.jpg", self.source))


if __name__ == "__main__":
    unittest.main()


class TestCoverFreshnessVerdictCaching(unittest.TestCase):
    """The freshness check must not run on every cover request.

    The dashboard requests one cover per mapping, and confirming freshness costs a
    DB lookup plus resolve_book_path — which rglobs the whole library whenever its
    100-entry path cache misses, and a real library has far more books than that.
    """

    def setUp(self):
        import src.web_server as web_server

        self.web_server = web_server
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.covers = Path(self.tmp.name)
        self.cover = self.covers / "abc.jpg"
        self.cover.write_bytes(b"jpg")

        # COVERS_DIR/database_service are assigned by setup_dependencies() at boot,
        # so they may not exist as module attributes in a bare unit test.
        self._saved_covers = getattr(web_server, "COVERS_DIR", None)
        self._saved_db = getattr(web_server, "database_service", None)
        web_server.COVERS_DIR = self.covers
        web_server._cover_fresh_until.clear()
        self.addCleanup(web_server._cover_fresh_until.clear)

        self.lookups = []

        class _DB:
            def get_book_by_kosync_id(_self, doc_hash):
                self.lookups.append(doc_hash)
                return None  # no mapping -> nothing to compare, verdict is "fresh"

        web_server.database_service = _DB()

        # The ebook check runs on a background pool; run it inline to count it.
        class _InlinePool:
            def submit(_self, fn, *args):
                fn(*args)

        self._saved_pool = web_server._cover_recheck_pool
        web_server._cover_recheck_pool = _InlinePool()

        def _restore():
            web_server.COVERS_DIR = self._saved_covers
            web_server.database_service = self._saved_db
            web_server._cover_recheck_pool = self._saved_pool

        self.addCleanup(_restore)

    def _serve(self):
        from flask import Flask

        app = Flask(__name__)
        with app.test_request_context("/"):
            return self.web_server.serve_cover("abc.jpg")

    def test_repeat_requests_do_not_re_resolve_the_ebook(self):
        self._serve()
        self._serve()
        self._serve()

        self.assertEqual(
            len(self.lookups), 1,
            "the freshness verdict must be cached, not recomputed per request",
        )

    def test_expired_verdict_is_rechecked(self):
        self._serve()
        self.assertEqual(len(self.lookups), 1)

        # Expire the cached verdict; an ebook edited later must still be noticed.
        self.web_server._cover_fresh_until["abc"] = time.time() - 1
        self._serve()

        self.assertEqual(
            len(self.lookups), 2,
            "an expired verdict must fall through to a fresh check",
        )
