"""Regression tests for non-blocking, source-preserving Library thumbnails."""

import os
import shutil
import struct
import subprocess
import threading
import unittest
import zlib
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.services.cover_thumbnail_service import CoverThumbnailCache


def _png(width: int, height: int, red: int = 30) -> bytes:
    """Small real PNG, constructed without a Pillow dependency."""
    def chunk(name, payload):
        return struct.pack(">I", len(payload)) + name + payload + struct.pack(
            ">I", zlib.crc32(name + payload) & 0xFFFFFFFF
        )

    pixels = (b"\x00" + bytes([red, 95, 180]) * width) * height
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(pixels))
            + chunk(b"IEND", b""))


class TestCoverThumbnailCache(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.source = root / "book.jpg"  # intentionally mismatched PNG extension
        self.target = root / "thumbs" / "book.jpg"
        self.source.write_bytes(_png(160, 200))
        self.cache = CoverThumbnailCache(workers=1)
        self.addCleanup(lambda: self.cache._pool.shutdown(wait=True))

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg required for image generation")
    def test_real_png_with_jpg_extension_gets_thumb_without_touching_source(self):
        original = self.source.read_bytes()
        self.assertTrue(self.cache.generate(self.source, self.target))
        self.assertTrue(self.target.read_bytes().startswith(b"\xff\xd8"))
        self.assertTrue(self.cache.is_current(self.source, self.target))
        self.assertEqual(self.source.read_bytes(), original)

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg required for image generation")
    def test_same_mtime_but_changed_size_invalidates(self):
        self.assertTrue(self.cache.generate(self.source, self.target))
        before = self.source.stat()
        self.source.write_bytes(_png(160, 200, red=200) + b"\x00")
        os.utime(self.source, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertFalse(self.cache.is_current(self.source, self.target))
        self.assertTrue(self.cache.generate(self.source, self.target))
        self.assertTrue(self.cache.is_current(self.source, self.target))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg tools required")
    def test_large_artwork_is_bounded_and_proportional(self):
        self.source.write_bytes(_png(1000, 1400))
        self.assertTrue(self.cache.generate(self.source, self.target))
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0", str(self.target)],
            check=True, text=True, capture_output=True,
        )
        width, height = (int(value) for value in result.stdout.strip().split(","))
        self.assertLessEqual(width, 720)
        self.assertLessEqual(height, 960)
        self.assertAlmostEqual(width / height, 1000 / 1400, delta=0.01)

    def test_source_changing_during_encoder_is_not_published(self):
        def racing_encoder(command, **_kwargs):
            Path(command[-1]).write_bytes(b"encoded bytes")
            self.source.write_bytes(self.source.read_bytes() + b"changed")

        with patch("src.services.cover_thumbnail_service.subprocess.run", side_effect=racing_encoder):
            self.assertFalse(self.cache.generate(self.source, self.target))
        self.assertFalse(self.target.exists())

    def test_missing_or_corrupt_metadata_never_authorizes_stale_thumb(self):
        self.target.parent.mkdir()
        self.target.write_bytes(b"not a valid image")
        self.assertFalse(self.cache.is_current(self.source, self.target))
        self.target.with_suffix(".json").write_text("not JSON", encoding="utf-8")
        self.assertFalse(self.cache.is_current(self.source, self.target))

    def test_failed_encode_is_throttled_until_source_changes(self):
        with patch.object(self.cache, "generate", return_value=False) as generate:
            self.assertTrue(self.cache.enqueue(self.source, self.target))
            self.cache._pool.shutdown(wait=True)
            self.assertTrue(self.cache.failed_recently(self.source, self.target))
            self.assertFalse(self.cache.enqueue(self.source, self.target))
            self.assertEqual(generate.call_count, 1)
        self.source.write_bytes(self.source.read_bytes() + b"changed")
        self.assertFalse(self.cache.failed_recently(self.source, self.target))

    def test_source_rewritten_during_failure_is_not_throttled(self):
        def changed_during_encode(_source, _target):
            self.source.write_bytes(self.source.read_bytes() + b"new")
            return False

        with patch.object(self.cache, "generate", side_effect=changed_during_encode):
            self.assertTrue(self.cache.enqueue(self.source, self.target))
            self.cache._pool.shutdown(wait=True)
        self.assertFalse(self.cache.failed_recently(self.source, self.target))

    def test_duplicate_pending_generation_is_suppressed(self):
        entered = threading.Event()
        release = threading.Event()

        def slow_generate(*args):
            entered.set()
            release.wait(5)
            return True

        with patch.object(self.cache, "generate", side_effect=slow_generate) as generated:
            self.assertTrue(self.cache.enqueue(self.source, self.target))
            self.assertTrue(entered.wait(3))
            self.assertFalse(self.cache.enqueue(self.source, self.target))
            release.set()
            self.cache._pool.shutdown(wait=True)
            self.assertEqual(generated.call_count, 1)


class TestThumbnailCoverRoute(unittest.TestCase):
    def setUp(self):
        from src import web_server
        self.web = web_server
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.covers = Path(self.temp.name)
        (self.covers / "abc.jpg").write_bytes(b"source artwork")
        old_dir = getattr(self.web, "COVERS_DIR", None)
        old_db = getattr(self.web, "database_service", None)

        class DB:
            def get_book_by_kosync_id(self, _id):
                return None

        self.web.COVERS_DIR = self.covers
        self.web.database_service = DB()
        self.web._cover_fresh_until.clear()
        self.web._thumb_versions.clear()

        def cleanup():
            self.web.COVERS_DIR = old_dir
            self.web.database_service = old_db
            self.web._cover_fresh_until.clear()
            self.web._thumb_versions.clear()
        self.addCleanup(cleanup)

    def test_cold_thumb_returns_small_pending_response_without_source_bytes(self):
        from flask import Flask
        app = Flask(__name__)
        with patch.object(self.web.cover_thumbnail_cache, "is_current", return_value=False), \
                patch.object(self.web.cover_thumbnail_cache, "enqueue", return_value=True) as enqueue:
            with app.test_request_context("/covers/thumb/abc.jpg"):
                response = self.web.serve_thumbnail_cover("abc.jpg")
                self.assertEqual(response.status_code, 202)
                self.assertEqual(response.get_data(), b"")
                self.assertEqual(response.headers["Cache-Control"], "private, no-store")
                self.assertEqual(response.headers["Retry-After"], "1")
                enqueue.assert_called_once_with(self.covers / "abc.jpg", self.covers / "thumbs" / "abc.jpg")

    def test_pending_head_does_not_download_source_image(self):
        from flask import Flask
        app = Flask(__name__)
        with patch.object(self.web.cover_thumbnail_cache, "is_current", return_value=False), \
                patch.object(self.web.cover_thumbnail_cache, "enqueue", return_value=True):
            with app.test_request_context("/covers/thumb/abc.jpg", method="HEAD"):
                response = self.web.serve_thumbnail_cover("abc.jpg")
                self.assertEqual(response.status_code, 202)
                self.assertEqual(response.get_data(), b"")

    def test_failed_thumbnail_returns_retryable_error_for_original_fallback(self):
        from flask import Flask
        app = Flask(__name__)
        with patch.object(self.web.cover_thumbnail_cache, "is_current", return_value=False), \
                patch.object(self.web.cover_thumbnail_cache, "failed_recently", return_value=True), \
                patch.object(self.web.cover_thumbnail_cache, "enqueue") as enqueue:
            with app.test_request_context("/covers/thumb/abc.jpg", method="HEAD"):
                response = self.web.serve_thumbnail_cover("abc.jpg")
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.headers["Cache-Control"], "private, no-store")
                enqueue.assert_not_called()

    def test_hot_thumb_serves_generated_file_and_preserves_private_cache(self):
        from flask import Flask
        app = Flask(__name__)
        (self.covers / "thumbs").mkdir()
        (self.covers / "thumbs" / "abc.jpg").write_bytes(b"small image")
        with patch.object(self.web.cover_thumbnail_cache, "is_current", return_value=True), \
                patch.object(self.web.cover_thumbnail_cache, "enqueue") as enqueue:
            with app.test_request_context("/covers/thumb/abc.jpg"):
                response = self.web.serve_thumbnail_cover("abc.jpg")
                response.direct_passthrough = False
                try:
                    self.assertEqual(response.get_data(), b"small image")
                finally:
                    response.close()
                self.assertEqual(response.headers["Cache-Control"], "private, max-age=300")
                enqueue.assert_not_called()

    def test_hot_thumbnail_head_reports_ready_without_content(self):
        from flask import Flask
        app = Flask(__name__)
        (self.covers / "thumbs").mkdir()
        (self.covers / "thumbs" / "abc.jpg").write_bytes(b"small image")
        with patch.object(self.web.cover_thumbnail_cache, "is_current", return_value=True):
            with app.test_request_context("/covers/thumb/abc.jpg", method="HEAD"):
                response = self.web.serve_thumbnail_cover("abc.jpg")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["Cache-Control"], "private, max-age=300")
                response.close()

    def _hot_get(self, url):
        from flask import Flask
        app = Flask(__name__)
        with patch.object(self.web.cover_thumbnail_cache, "is_current", return_value=True):
            with app.test_request_context(url, method="HEAD"):
                response = self.web.serve_thumbnail_cover("abc.jpg")
                response.close()
                return response

    def test_served_thumb_gets_versioned_url_cached_for_a_year(self):
        (self.covers / "thumbs").mkdir()
        (self.covers / "thumbs" / "abc.jpg").write_bytes(b"small image")
        self.assertIsNone(self.web.thumb_cover_src("abc"))
        self._hot_get("/covers/thumb/abc.jpg")
        src = self.web.thumb_cover_src("abc")
        self.assertRegex(src, r"^/covers/thumb/abc\.jpg\?v=[0-9a-f]+-[0-9a-f]+$")
        response = self._hot_get(src)
        self.assertEqual(response.headers["Cache-Control"], "private, max-age=31536000, immutable")
        # A stale version must not be pinned in the browser for a year.
        response = self._hot_get("/covers/thumb/abc.jpg?v=0-0")
        self.assertEqual(response.headers["Cache-Control"], "private, max-age=300")

    def test_regenerated_thumb_changes_version(self):
        (self.covers / "thumbs").mkdir()
        thumb = self.covers / "thumbs" / "abc.jpg"
        thumb.write_bytes(b"small image")
        self._hot_get("/covers/thumb/abc.jpg")
        before = self.web.thumb_cover_src("abc")
        thumb.write_bytes(b"a different, larger image")
        self._hot_get("/covers/thumb/abc.jpg")
        self.assertNotEqual(self.web.thumb_cover_src("abc"), before)

    def test_cold_or_expired_thumb_is_not_rendered_directly(self):
        from flask import Flask
        app = Flask(__name__)
        self.web._thumb_versions["abc"] = ("1-2", 0)
        self.assertIsNone(self.web.thumb_cover_src("abc"))
        self.web._thumb_versions["abc"] = ("1-2", float("inf"))
        with patch.object(self.web.cover_thumbnail_cache, "is_current", return_value=False), \
                patch.object(self.web.cover_thumbnail_cache, "enqueue", return_value=True):
            with app.test_request_context("/covers/thumb/abc.jpg", method="HEAD"):
                self.assertEqual(self.web.serve_thumbnail_cover("abc.jpg").status_code, 202)
        self.assertIsNone(self.web.thumb_cover_src("abc"))
        self.assertIsNone(self.web.thumb_cover_src(None))

    def test_reject_nested_and_non_cover_paths(self):
        from flask import Flask
        from werkzeug.exceptions import NotFound
        app = Flask(__name__)
        with app.test_request_context("/covers/thumb/../abc.jpg"):
            with self.assertRaises(NotFound):
                self.web.serve_thumbnail_cover("../abc.jpg")


if __name__ == "__main__":
    unittest.main()
