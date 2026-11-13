"""Cover requests must never wait on the ebook library.

Measured on a live library (network mount): finding one book's ebook to check its
cover took 0.2-8 s, and it ran inside every first cover request, so a page of covers
appeared after several seconds. The check now runs in the background, thumbnails
keep their direct URLs across restarts, and HTML pages are gzipped.
"""

import gzip
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from flask import Flask


class _RecordingPool:
    """Stands in for the background pool: records work instead of running it."""

    def __init__(self):
        self.submitted = []

    def submit(self, fn, *args):
        self.submitted.append((fn, args))


class _CoverTestCase(unittest.TestCase):
    def setUp(self):
        from src import web_server

        self.web = web_server
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.covers = Path(self.temp.name) / "covers"
        (self.covers / "thumbs").mkdir(parents=True)
        self.lookups = []
        self.book = None
        case = self

        class DB:
            def get_book_by_kosync_id(self, doc_hash):
                case.lookups.append(doc_hash)
                return case.book

        self.pool = _RecordingPool()
        saved = {
            name: getattr(web_server, name, None)
            for name in ("COVERS_DIR", "database_service", "container",
                         "_cover_recheck_pool", "_thumb_versions_seeded_dir")
        }
        web_server.COVERS_DIR = self.covers
        web_server.database_service = DB()
        web_server._cover_recheck_pool = self.pool
        web_server._cover_fresh_until.clear()
        web_server._thumb_versions.clear()

        def restore():
            for name, value in saved.items():
                setattr(web_server, name, value)
            web_server._cover_fresh_until.clear()
            web_server._thumb_versions.clear()

        self.addCleanup(restore)
        self.app = Flask(__name__)

    def _serve(self, name="abc.jpg"):
        with self.app.test_request_context(f"/covers/{name}"):
            return self.web.serve_cover(name)


class TestCoverRequestNeverScansTheLibrary(_CoverTestCase):
    def test_existing_cover_is_served_without_any_ebook_lookup(self):
        (self.covers / "abc.jpg").write_bytes(b"jpg")
        response = self._serve()
        self.assertEqual(response.status_code, 200)
        response.close()
        self.assertEqual(self.lookups, [], "the ebook lookup must not run on the request")
        self.assertEqual(self.pool.submitted, [(self.web._recheck_cover, ("abc",))])

    def test_background_check_is_queued_once_per_window(self):
        (self.covers / "abc.jpg").write_bytes(b"jpg")
        for _ in range(3):
            self._serve().close()
        self.assertEqual(len(self.pool.submitted), 1)
        self.web._cover_fresh_until["abc"] = time.time() - 1
        self._serve().close()
        self.assertEqual(len(self.pool.submitted), 2, "an expired window must queue a new check")

    def test_rendering_a_direct_thumbnail_url_queues_the_check(self):
        (self.covers / "abc.jpg").write_bytes(b"jpg")
        (self.covers / "thumbs" / "abc.jpg").write_bytes(b"thumb")
        self.assertIsNotNone(self.web.thumb_cover_src("abc"))
        self.web.thumb_cover_src("abc")
        self.assertEqual(self.pool.submitted, [(self.web._recheck_cover, ("abc",))])

    def test_missing_cover_does_not_retry_extraction_on_every_request(self):
        self.assertEqual(self._serve(), ("Cover not found", 404))
        self.assertEqual(self._serve(), ("Cover not found", 404))
        self.assertEqual(len(self.lookups), 1, "a failed extraction must be remembered")
        self.web._cover_fresh_until["abc"] = time.time() - 1
        self._serve()
        self.assertEqual(len(self.lookups), 2, "extraction must be retried after the window")

    def test_missing_cover_is_still_extracted_on_request(self):
        self.book = SimpleNamespace(ebook_filename="book.epub", abs_title="A Book")
        parser = MagicMock()
        parser.resolve_book_path.return_value = Path("/books/book.epub")

        def extract(_source, cover_path):
            Path(cover_path).write_bytes(b"jpg")
            return True

        parser.extract_cover.side_effect = extract
        self.web.container = SimpleNamespace(ebook_parser=lambda: parser)
        response = self._serve()
        self.assertEqual(response.status_code, 200)
        response.close()
        parser.extract_cover.assert_called_once_with(Path("/books/book.epub"), self.covers / "abc.jpg")


class TestBackgroundCoverRecheck(_CoverTestCase):
    def setUp(self):
        super().setUp()
        self.cover = self.covers / "abc.jpg"
        self.cover.write_bytes(b"old cover")
        self.ebook = Path(self.temp.name) / "book.epub"
        self.ebook.write_bytes(b"epub")
        self.book = SimpleNamespace(ebook_filename="book.epub", abs_title="A Book")
        self.parser = MagicMock()
        self.parser.resolve_book_path.return_value = self.ebook
        self.web.container = SimpleNamespace(ebook_parser=lambda: self.parser)

    def _age(self, path, seconds):
        stamp = time.time() - seconds
        os.utime(path, (stamp, stamp))

    def test_changed_ebook_re_extracts_the_cover(self):
        self._age(self.cover, 3600)
        with self.assertLogs("src.web_server", level="INFO") as logs:
            self.web._recheck_cover("abc")
        self.parser.extract_cover.assert_called_once_with(self.ebook, self.cover)
        self.assertIn(
            "🖼️ Re-extracting cover for 'A Book': the ebook changed since it was cached",
            "\n".join(logs.output),
        )

    def test_unchanged_ebook_leaves_the_cover_alone(self):
        self._age(self.ebook, 3600)
        self.web._recheck_cover("abc")
        self.parser.extract_cover.assert_not_called()

    def test_outdated_thumbnail_loses_its_direct_url_and_is_rebuilt(self):
        self._age(self.ebook, 3600)
        self.web._thumb_versions["abc"] = "1-2"
        with patch.object(self.web.cover_thumbnail_cache, "is_current", return_value=False), \
                patch.object(self.web.cover_thumbnail_cache, "enqueue") as enqueue:
            self.web._recheck_cover("abc")
        self.assertNotIn("abc", self.web._thumb_versions)
        enqueue.assert_called_once_with(self.cover, self.covers / "thumbs" / "abc.jpg")

    def test_current_thumbnail_keeps_its_direct_url(self):
        self._age(self.ebook, 3600)
        self.web._thumb_versions["abc"] = "1-2"
        with patch.object(self.web.cover_thumbnail_cache, "is_current", return_value=True), \
                patch.object(self.web.cover_thumbnail_cache, "enqueue") as enqueue:
            self.web._recheck_cover("abc")
        self.assertEqual(self.web._thumb_versions["abc"], "1-2")
        enqueue.assert_not_called()

    def test_a_failing_check_is_logged_not_raised(self):
        self.parser.resolve_book_path.return_value = self.ebook
        self._age(self.cover, 3600)
        self.parser.extract_cover.side_effect = RuntimeError("bad epub")
        with self.assertLogs("src.web_server", level="WARNING"):
            self.web._recheck_cover("abc")


class TestThumbnailUrlsSurviveRestart(_CoverTestCase):
    def test_thumbnail_on_disk_renders_a_direct_url_in_a_fresh_process(self):
        thumb = self.covers / "thumbs" / "abc.jpg"
        thumb.write_bytes(b"small image")
        stat = thumb.stat()
        self.assertEqual(
            self.web.thumb_cover_src("abc"),
            f"/covers/thumb/abc.jpg?v={stat.st_size:x}-{stat.st_mtime_ns:x}",
        )

    def test_empty_or_absent_thumbnail_is_probed_instead(self):
        (self.covers / "thumbs" / "empty.jpg").write_bytes(b"")
        (self.covers / "thumbs" / "abc.json").write_text("[1, 2]", encoding="utf-8")
        self.assertIsNone(self.web.thumb_cover_src("empty"))
        self.assertIsNone(self.web.thumb_cover_src("abc"))
        self.assertIsNone(self.web.thumb_cover_src(None))

    def test_folder_is_read_once_per_process(self):
        (self.covers / "thumbs" / "abc.jpg").write_bytes(b"small image")
        self.web.thumb_cover_src("abc")
        with patch.object(self.web.os, "scandir", side_effect=AssertionError("rescanned")):
            self.assertIsNotNone(self.web.thumb_cover_src("abc"))

    def test_missing_thumbnail_folder_is_harmless(self):
        shutil.rmtree(self.covers / "thumbs")
        self.assertIsNone(self.web.thumb_cover_src("abc"))


class TestHtmlIsGzipped(unittest.TestCase):
    def setUp(self):
        from src import web_server

        self.web = web_server
        self.app = Flask(__name__)
        self.page = "<html><body>" + "<p>library card</p>" * 400 + "</body></html>"

        @self.app.route("/page")
        def page():
            return self.page

        @self.app.route("/tiny")
        def tiny():
            return "<html></html>"

        @self.app.route("/data")
        def data():
            return {"cards": ["x" * 4000]}

        self.app.after_request(web_server.gzip_html_response)
        self.client = self.app.test_client()

    def test_html_is_compressed_for_a_browser_that_accepts_gzip(self):
        response = self.client.get("/page", headers={"Accept-Encoding": "gzip, deflate"})
        self.assertEqual(response.headers["Content-Encoding"], "gzip")
        self.assertIn("Accept-Encoding", response.headers["Vary"])
        self.assertLess(len(response.data), len(self.page) // 5)
        self.assertEqual(gzip.decompress(response.data).decode(), self.page)
        self.assertEqual(int(response.headers["Content-Length"]), len(response.data))

    def test_html_is_untouched_when_gzip_is_refused(self):
        for header in ("gzip;q=0", "br, gzip;q=0", "gzip;q=0, *", "*, gzip;q=0"):
            with self.subTest(header=header):
                response = self.client.get("/page", headers={"Accept-Encoding": header})
                self.assertNotIn("Content-Encoding", response.headers)
                self.assertEqual(response.get_data(as_text=True), self.page)

    def test_html_is_compressed_for_a_low_but_positive_gzip_weight(self):
        response = self.client.get("/page", headers={"Accept-Encoding": "deflate, gzip;q=0.5"})
        self.assertEqual(response.headers["Content-Encoding"], "gzip")
        self.assertEqual(gzip.decompress(response.data).decode(), self.page)

    def test_html_is_untouched_without_gzip_support(self):
        response = self.client.get("/page")
        self.assertNotIn("Content-Encoding", response.headers)
        self.assertEqual(response.get_data(as_text=True), self.page)

    def test_small_pages_and_non_html_are_left_alone(self):
        for url in ("/tiny", "/data"):
            response = self.client.get(url, headers={"Accept-Encoding": "gzip"})
            self.assertNotIn("Content-Encoding", response.headers, url)

    def test_compression_runs_after_the_csrf_script_is_added(self):
        from tests.test_webserver import MockContainer

        temp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, temp, ignore_errors=True)
        saved = {key: os.environ.get(key) for key in ("DATA_DIR", "BOOKS_DIR")}
        os.environ["DATA_DIR"] = os.environ["BOOKS_DIR"] = temp

        def restore():
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.addCleanup(restore)
        app, _ = self.web.create_app(test_container=MockContainer())
        hooks = app.after_request_funcs[None]
        # Flask runs after_request hooks in reverse registration order.
        self.assertLess(
            hooks.index(self.web.gzip_html_response),
            hooks.index(self.web.inject_csrf_script),
        )


if __name__ == "__main__":
    unittest.main()
