"""Book detail on the stats page carries the book's own listening sessions."""

import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock

from tests.test_webserver import MockContainer

LISTENING_DETAIL = {
    "totalSeconds": 900,
    "sessionCount": 2,
    "avgSessionSeconds": 450,
    "lastReadAt": 1751475900,
    "bookTitle": "Snapshot Title",
    "sessions": [
        {"startTime": 1751475600, "endTime": 1751475900, "durationSeconds": 300,
         "startProgress": 0.4, "endProgress": 0.45},
        {"startTime": 1751439000, "endTime": 1751439600, "durationSeconds": 600,
         "startProgress": None, "endProgress": None},
    ],
}


class TestStatsBookDetail(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        os.environ["DATA_DIR"] = self.temp_dir
        os.environ["BOOKS_DIR"] = self.temp_dir
        self.mock_container = MockContainer()
        self.db = self.mock_container.mock_database_service
        self.db.get_all_books.return_value = []
        self.db.get_all_reading_stats.return_value = {}

        import src.db.migration_utils
        self._orig_init_db = src.db.migration_utils.initialize_database
        src.db.migration_utils.initialize_database = lambda data_dir: self.db

        from src.web_server import create_app
        self.app, _ = create_app(test_container=self.mock_container)
        self.app.config["TESTING"] = True

        import src.web_server as ws
        self.ws = ws

    def tearDown(self):
        import src.db.migration_utils
        src.db.migration_utils.initialize_database = self._orig_init_db
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _detail(self, key):
        with self.app.test_request_context(f"/api/stats/book-detail?key={key}"):
            response = self.ws.api_stats_book_detail()
        if isinstance(response, tuple):
            return response[0].get_json(), response[1]
        return response.get_json(), response.status_code

    def test_listening_only_book_returns_its_sessions(self):
        self.db.get_koreader_book_detail.return_value = None
        self.db.get_book_listening_detail.return_value = dict(LISTENING_DETAIL)
        self.db.get_book.return_value = MagicMock(abs_title="My Brilliant Friend")

        payload, status = self._detail("abs:b1")

        self.assertEqual(status, 200)
        self.db.get_book_listening_detail.assert_called_once_with("b1")
        listening = payload["listening"]
        self.assertEqual(payload["title"], "My Brilliant Friend")
        self.assertEqual(listening["absId"], "b1")
        self.assertEqual(listening["sessionCount"], 2)
        self.assertEqual(listening["sessions"], LISTENING_DETAIL["sessions"])
        self.assertNotIn("bookTitle", listening)

    def test_listening_title_falls_back_to_session_snapshot(self):
        self.db.get_koreader_book_detail.return_value = None
        self.db.get_book_listening_detail.return_value = dict(LISTENING_DETAIL)
        self.db.get_book.return_value = None

        payload, _ = self._detail("abs:b1")

        self.assertEqual(payload["title"], "Snapshot Title")

    def test_reading_book_links_listening_through_its_abs_id(self):
        self.db.get_koreader_book_detail.return_value = {
            "title": "Read Title", "absId": "b2", "sessions": [],
        }
        self.db.get_book_listening_detail.return_value = None

        payload, status = self._detail("koreader:abc")

        self.assertEqual(status, 200)
        self.db.get_book_listening_detail.assert_called_once_with("b2")
        self.assertIsNone(payload["listening"])
        self.assertEqual(payload["title"], "Read Title")

    def test_book_without_any_activity_is_not_found(self):
        self.db.get_koreader_book_detail.return_value = None
        self.db.get_book_listening_detail.return_value = None
        self.db.get_reading_stats.return_value = None

        _, status = self._detail("abs:missing")

        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
