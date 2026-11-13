"""The inactive-cache admin endpoint must never delete unverified data."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask
from src import web_server


class TestCacheCleanupPreview(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.caches = self.root / "audio_cache"
        self.caches.mkdir()
        self.active = self._create("bookorbit_123", 11)
        self.retryable = self._create("booklore_200", 13)
        self.inactive = self._create("bookorbit_555", 17)
        self.unknown = self._create("mystery", 23)
        self.books = [
            SimpleNamespace(abs_id="bookorbit:123", status="active"),
            SimpleNamespace(abs_id="booklore:200", status="failed_retry_later"),
            SimpleNamespace(abs_id="bookorbit:555", status="inactive"),
        ]
        self.jobs = [SimpleNamespace(abs_id="bookorbit:123")]

        class Database:
            def __init__(this, books, jobs):
                this.books = books
                this.jobs = jobs

            def get_all_books(this):
                return this.books

            def get_all_jobs(this):
                return this.jobs

        self.db = Database(self.books, self.jobs)
        self.saved_dir = getattr(web_server, "DATA_DIR", None)
        self.saved_db = getattr(web_server, "database_service", None)
        web_server.DATA_DIR = self.root
        web_server.database_service = self.db
        self.addCleanup(self._restore)
        self.app = Flask(__name__)

    def _restore(self):
        web_server.DATA_DIR = self.saved_dir
        web_server.database_service = self.saved_db

    def _create(self, key, size):
        path = self.caches / key / "source_tracks" / "audio.m4b"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"x" * size)
        return path

    def test_preview_keeps_every_file_and_classifies_proven_inactive_only(self):
        with patch.object(web_server.shutil, "rmtree", side_effect=AssertionError("deletion attempted")):
            with self.app.test_request_context("/api/cache/clean", method="POST", json={"dry_run": True}):
                response = web_server.clean_inactive_cache()
                data = response.get_json()
        self.assertTrue(data["success"])
        self.assertTrue(data["dry_run"])
        self.assertTrue(data["cleanup_disabled"])
        self.assertEqual(data["potential_inactive_audio_bytes"], 17)
        self.assertEqual(data["potential_inactive_audio"], [
            {"cache_name": "bookorbit_555", "bytes": 17}
        ])
        self.assertEqual(data["unverified_audio_directories"], 1)
        self.assertEqual(data["unverified_audio_bytes"], 23)
        self.assertEqual(data["protected_audio_directories"], 3)
        self.assertEqual(data["deleted_audio"], 0)
        for p in (self.active, self.retryable, self.inactive, self.unknown):
            self.assertTrue(p.is_file(), p)

    def test_explicit_deletion_request_is_rejected_without_touching_any_file(self):
        with patch.object(web_server.shutil, "rmtree", side_effect=AssertionError("deletion attempted")):
            with self.app.test_request_context("/api/cache/clean", method="POST", json={"dry_run": False}):
                response, status = web_server.clean_inactive_cache()
        self.assertEqual(status, 409)
        self.assertFalse(response.get_json()["success"])
        for p in (self.active, self.retryable, self.inactive, self.unknown):
            self.assertTrue(p.is_file())

    def test_blank_legacy_post_is_still_safe_and_returns_a_preview(self):
        with self.app.test_request_context("/api/cache/clean", method="POST"):
            data = web_server.clean_inactive_cache().get_json()
        self.assertEqual(data["deleted_audio"], 0)
        self.assertTrue(data["dry_run"])

    def test_cache_route_stays_admin_only(self):
        self.assertIn("clean_cache", web_server._ADMIN_ONLY_ENDPOINTS)


if __name__ == "__main__":
    unittest.main()
