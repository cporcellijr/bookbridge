"""ABS API tokens in `?token=` URLs must never reach logs, jobs or diagnostics."""

import logging
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.db.models import Book
from src.services.diagnostics import scrub_diagnostic_text
from src.sync_manager import SyncManager
from src.utils.logging_utils import (
    MemoryLogHandler,
    ThresholdMojibakeFormatter,
    redact_url_secrets,
)

SECRET = "SECRET123"
JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1c2VySWQiOiJyb290In0."
    "Q2x-aW_9kR3b7Yq1Zs0pL8mN4vT6uXc2EeFfGgHhIiJ"
)
ERR = (
    "HTTPConnectionPool(host='127.0.0.1', port=9): Max retries exceeded with url: "
    f"/api/items/li_abc/cover?token={SECRET} (Caused by X)"
)


class TestRedactHelper(unittest.TestCase):
    def test_redacts_secret_keys(self):
        out = redact_url_secrets(f"u?a=1&Token={SECRET}&api_key=k1&apikey=k2&access_token=k3 end")
        for leaked in (SECRET, "k1", "k2", "k3"):
            self.assertNotIn(leaked, out)
        self.assertIn("a=1", out)
        self.assertTrue(out.endswith(" end"))

    def test_stops_at_delimiters(self):
        self.assertEqual(redact_url_secrets(f"({ERR})").count("(Caused by X)"), 1)
        self.assertIn("token=[redacted] (Caused", redact_url_secrets(ERR))

    def test_leaves_normal_text_untouched(self):
        text = "page=2&limit=5 path /api/items?sort=title token_count=3 a=b"
        self.assertEqual(redact_url_secrets(text), text)


class TestLoggingRedaction(unittest.TestCase):
    def _record(self):
        try:
            raise RuntimeError(ERR)
        except RuntimeError:
            import sys
            return logging.LogRecord(
                "t", logging.ERROR, __file__, 1, "failed: %s", (ERR,), sys.exc_info()
            )

    def test_formatter_redacts_message_and_traceback(self):
        out = ThresholdMojibakeFormatter("%(message)s").format(self._record())
        self.assertNotIn(SECRET, out)
        self.assertGreater(out.count("token=[redacted]"), 1)

    def test_memory_handler_stores_redacted_message(self):
        handler = MemoryLogHandler()
        handler.emit(self._record())
        self.assertNotIn(SECRET, handler.logs[0]["message"])

    def test_diagnostics_scrub_drops_jwt_signature(self):
        out = scrub_diagnostic_text(
            f"Max retries exceeded with url: /api/items/li_abc/cover?token={JWT} (Caused by X)"
        )
        self.assertNotIn("Q2x-aW_9kR3b7Yq1Zs0pL8mN4vT6uXc2EeFfGgHhIiJ", out)


class TestJobLastError(unittest.TestCase):
    def test_background_failure_saves_redacted_last_error(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        manager = SyncManager.__new__(SyncManager)
        for name in ("abs_client", "booklore_client", "hardcover_client", "transcriber",
                     "ebook_parser", "database_service", "storyteller_client",
                     "alignment_service"):
            setattr(manager, name, MagicMock())
        manager.library_service = None
        manager.migration_service = None
        manager.data_dir = manager.books_dir = tmp
        manager.epub_cache_dir = tmp / "epub_cache"
        manager._get_local_epub = MagicMock(side_effect=RuntimeError(ERR))
        manager.database_service.get_latest_job.return_value = SimpleNamespace(
            retry_count=0, last_error=None, progress=0.0, stage=None
        )
        book = Book(abs_id="ebook-abc123", abs_title="T", ebook_filename="b.epub",
                    kosync_doc_id="h", sync_mode="ebook_only", status="processing")

        manager._run_background_job(book, 1, 1)

        saved = manager.database_service.save_job.call_args[0][0]
        self.assertIn("Max retries exceeded", saved.last_error)
        self.assertNotIn(SECRET, saved.last_error)


class TestProxyCoverNoQueryToken(unittest.TestCase):
    def setUp(self):
        from tests.test_webserver import MockContainer
        from src.db.database_service import DatabaseService
        import src.db.migration_utils

        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        os.environ['DATA_DIR'] = self.tmp
        os.environ['BOOKS_DIR'] = self.tmp
        os.environ['TEMPLATE_DIR'] = str(Path(__file__).parent.parent / "templates")
        self.svc = DatabaseService(os.path.join(self.tmp, "c.db"))
        container = MockContainer()
        container.mock_database_service = self.svc
        orig = src.db.migration_utils.initialize_database
        src.db.migration_utils.initialize_database = lambda data_dir: self.svc
        self.addCleanup(setattr, src.db.migration_utils, "initialize_database", orig)

        from src.web_server import create_app
        self.app, _ = create_app(test_container=container)
        self.app.config['TESTING'] = True
        self.app.config['LOGIN_DISABLED'] = True
        self.client = self.app.test_client()

    def test_cover_request_uses_bearer_header_not_query_token(self):
        abs_client = MagicMock()
        abs_client.is_configured.return_value = True
        abs_client.token = SECRET
        abs_client.base_url = "http://abs.local"
        abs_client.headers = {"Authorization": f"Bearer {SECRET}"}
        upstream = MagicMock(status_code=200, headers={"content-type": "image/jpeg"})
        upstream.iter_content.return_value = iter([b"x"])
        with patch("src.web_server.requests") as req, \
                patch("src.web_server._user_may_modify_book", return_value=True), \
                patch("src.web_server.uc", return_value=SimpleNamespace(abs_client=abs_client)):
            req.get.return_value = upstream
            resp = self.client.get("/api/cover-proxy/li_abc")
            self.assertEqual(resp.status_code, 200)
            url = req.get.call_args[0][0]
            self.assertNotIn("token=", url)
            self.assertEqual(url, "http://abs.local/api/items/li_abc/cover")
            self.assertEqual(req.get.call_args[1]["headers"], abs_client.headers)


if __name__ == "__main__":
    unittest.main()
