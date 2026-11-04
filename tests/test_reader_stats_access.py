"""Real-auth coverage for reader stats access and per-user data isolation."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from flask.signals import template_rendered

REPO = Path(__file__).resolve().parent.parent
TEMPLATES = str(REPO / "templates")
STATIC = str(REPO / "static")

from tests.test_webserver import MockContainer
from src.db.database_service import DatabaseService
from src.db.models import Book


class TestReaderStatsAccess(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old_env = os.environ.copy()
        data_dir = Path(self.tmp) / "data"
        books_dir = Path(self.tmp) / "books"
        epub_cache_dir = Path(self.tmp) / "epub-cache"
        data_dir.mkdir()
        books_dir.mkdir()
        epub_cache_dir.mkdir()
        os.environ.update({"DATA_DIR": self.tmp, "BOOKS_DIR": self.tmp,
                           "TEMPLATE_DIR": TEMPLATES, "STATIC_DIR": STATIC})
        self.db = DatabaseService(os.path.join(self.tmp, "stats.db"))
        self.admin = self.db.create_user("admin", "secret", role="admin")
        self.reader = self.db.create_user("reader", "secret", role="user")
        for abs_id, title, owner, seconds in (("admin-book", "Admin Book", self.admin.id, 1800),
                                              ("reader-book", "Reader Book", self.reader.id, 900)):
            self.db.save_book(Book(abs_id=abs_id, abs_title=title, user_id=owner))
            self.db.record_reading_session(
                abs_id, "AUDIOBOOK", 1775570400, 1775570400 + seconds, seconds,
                start_progress=0.1, end_progress=0.2, user_id=owner,
            )

        self.container = MockContainer()
        self.container.data_dir = lambda: data_dir
        self.container.books_dir = lambda: books_dir
        self.container.epub_cache_dir = lambda: epub_cache_dir
        self.container.mock_database_service = self.db
        self.container.mock_user_client_registry.get_clients.side_effect = (
            lambda user_id: SimpleNamespace(abs_client=None)
        )
        import src.db.migration_utils
        self.old_init_db = src.db.migration_utils.initialize_database
        src.db.migration_utils.initialize_database = lambda _data_dir: self.db
        from src.web_server import create_app
        import src.web_server
        src.web_server.STATS_CACHE.clear()
        self.addCleanup(src.web_server.STATS_CACHE.clear)
        self.app, _ = create_app(test_container=self.container)
        self.app.config.update(TESTING=True, LOGIN_DISABLED=False)
        self.client = self.app.test_client()

    def tearDown(self):
        import src.db.migration_utils
        src.db.migration_utils.initialize_database = self.old_init_db
        import src.web_server
        src.web_server.STATS_CACHE.clear()
        os.environ.clear()
        os.environ.update(self.old_env)
        self.db.db_manager.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _login(self, client, user):
        response = client.post("/login", data={"username": user.username, "password": "secret"})
        self.assertEqual(response.status_code, 302)

    def test_stats_requires_login(self):
        self.assertEqual(self.client.get("/stats", follow_redirects=False).status_code, 302)
        for path in (
            "/api/stats",
            "/api/stats/reading-day?date=2026-04-07",
            "/api/stats/reading-calendar?month=2026-04",
            "/api/stats/book-detail?key=abs:reader-book",
            "/api/stats/yearly-recap?scope=listening&year=2026",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 401)

    def test_reader_can_load_stats_routes_and_only_sees_own_sessions(self):
        admin_client = self.app.test_client()
        self._login(admin_client, self.admin)
        admin_payload = admin_client.get("/api/stats").get_json()
        self.assertEqual(admin_payload["listening"]["stats"]["totalSeconds"], 1800)
        self.assertEqual({book["absId"] for book in admin_payload["listening"]["books"]}, {"admin-book"})
        self.container.mock_user_client_registry.get_clients.reset_mock()

        self._login(self.client, self.reader)
        rendered = []
        def capture(_sender, template, **_extra):
            rendered.append(template.name)
        with template_rendered.connected_to(capture, self.app):
            page = self.client.get("/stats")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(rendered[0], "stats.html")
        self.assertIn(b'href="/stats"', page.data, "Classic nav shows Stats to readers")
        payload = self.client.get("/api/stats").get_json()
        self.assertEqual(payload["listening"]["stats"]["totalSeconds"], 900)
        self.assertEqual({book["absId"] for book in payload["listening"]["books"]}, {"reader-book"})
        self.assertEqual(self.client.get("/api/stats/reading-day?date=2026-04-07").status_code, 200)
        self.assertEqual(self.client.get("/api/stats/reading-calendar?month=2026-04").status_code, 200)
        self.assertEqual(self.client.get("/api/stats/book-detail?key=abs:reader-book").status_code, 200)
        self.assertEqual(self.client.get("/api/stats/yearly-recap?scope=listening&year=2026").status_code, 200)
        requested_ids = [args[0][0] for args in self.container.mock_user_client_registry.get_clients.call_args_list]
        self.assertTrue(requested_ids)
        self.assertEqual(set(requested_ids), {self.reader.id})

    def test_reader_cannot_read_other_users_local_book_detail(self):
        self._login(self.client, self.reader)
        own = self.client.get("/api/stats/book-detail?key=abs:reader-book")
        other = self.client.get("/api/stats/book-detail?key=abs:admin-book")
        self.assertEqual(own.status_code, 200)
        self.assertEqual(other.status_code, 404)


if __name__ == "__main__":
    unittest.main()
