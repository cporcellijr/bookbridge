"""Stats endpoints use the request user's ABS bundle and per-user cache keys (#508)."""

import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from tests.test_webserver import MockContainer


def _user(user_id):
    return MagicMock(id=user_id, active=True)


class TestStatsUserScoping(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        os.environ["DATA_DIR"] = self.temp_dir
        os.environ["BOOKS_DIR"] = self.temp_dir
        self.mock_container = MockContainer()
        self.mock_abs_client = self.mock_container.mock_abs_client
        self.mock_database_service = self.mock_container.mock_database_service
        self.mock_database_service.get_all_books.return_value = []
        self.mock_database_service.get_all_reading_stats.return_value = {}

        import src.db.migration_utils
        self._orig_init_db = src.db.migration_utils.initialize_database
        src.db.migration_utils.initialize_database = lambda data_dir: self.mock_database_service

        from src.web_server import create_app
        self.app, _ = create_app(test_container=self.mock_container)
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

        import src.web_server as ws
        self.ws = ws
        ws.STATS_CACHE.clear()
        self.addCleanup(ws.STATS_CACHE.clear)
        # Global (admin) ABS client is unconfigured and yields nothing.
        self.mock_abs_client.is_configured.return_value = False
        self.mock_abs_client.get_listening_stats.return_value = None
        self.user_abs = MagicMock()
        self.user_abs.get_listening_stats.return_value = {
            "days": {"2026-04-07": 900}, "items": {"a": {}}, "recentSessions": [],
        }
        self.bundle = MagicMock(abs_client=self.user_abs)

    def tearDown(self):
        import src.db.migration_utils
        src.db.migration_utils.initialize_database = self._orig_init_db
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_listening_stats_use_request_user_bundle(self):
        with self.app.test_request_context('/api/stats'), \
                patch.object(self.ws, 'uc', return_value=self.bundle):
            payload = self.ws._build_listening_stats_payload(self.ws._get_stats_timezone())
        self.assertIsNotNone(payload)
        self.assertTrue(payload['available'])
        self.user_abs.get_listening_stats.assert_called()
        self.mock_abs_client.get_listening_stats.assert_not_called()

    def test_stats_cache_not_shared_between_users(self):
        with self.app.test_request_context('/api/stats'):
            with patch.object(self.ws, 'current_user', return_value=_user(1)):
                self.ws._write_cached_stats({"owner": "A"})
                self.assertEqual(self.ws._read_cached_stats(), {"owner": "A"})
            with patch.object(self.ws, 'current_user', return_value=_user(2)):
                self.assertIsNone(self.ws._read_cached_stats())

    def test_stats_cache_key_outside_request_does_not_raise(self):
        self.assertIn("anon", self.ws._build_stats_cache_key())

    def test_yearly_recap_uses_request_user_abs_client(self):
        self.mock_database_service.get_koreader_yearly_recap.return_value = {
            'year': 2026, 'months': [{'month': m, 'seconds': 0, 'pages': 0, 'finished': 0} for m in range(1, 13)],
            'totalSeconds': 0, 'totalPages': 0, 'booksFinished': 0, 'finishedBooks': [], 'availableYears': [],
        }
        with patch.object(self.ws, 'uc', return_value=self.bundle):
            response = self.client.get('/api/stats/yearly-recap?scope=listening&year=2026')
        self.assertEqual(response.status_code, 200)
        self.user_abs.get_listening_stats.assert_called()
        self.mock_abs_client.get_listening_stats.assert_not_called()


if __name__ == '__main__':
    unittest.main()
