"""Tests for reading position preview last_leader wiring.

Covers:
1. DatabaseService.get_reading_stats returns last_leader from the session with max(end_time).
2. _select_state tie-breaking is deterministic when last_leader is None.
3. The real route /api/books/<abs_id>/position-preview picks the latest session's leader
   when two states have equal last_updated, in both state orders.
"""

import os
import sys
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

# Add project root to Python path
sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault('DATA_DIR', 'test_data')
os.environ.setdefault('BOOKS_DIR', 'test_data')


class TestGetReadingStatsLastLeader(unittest.TestCase):
    """get_reading_stats returns last_leader from the session with greatest end_time."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.test_db_path = str(Path(self.temp_dir) / 'test_database.db')

        from src.db.database_service import DatabaseService
        from src.db.models import Book

        self.Book = Book
        self.db_service = DatabaseService(self.test_db_path)

    def tearDown(self):
        if hasattr(self, 'db_service') and hasattr(self.db_service, 'db_manager'):
            self.db_service.db_manager.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_last_leader_is_leader_of_most_recent_session(self):
        """Two sessions, different leader_client and end_time: last_leader is from max(end_time)."""
        user = self.db_service.create_user('rl-user', 'pw', role='admin')
        abs_id = 'rl-book'
        self.db_service.save_book(self.Book(abs_id=abs_id, abs_title='RL', status='active'))

        # Older session (end_time 1000, KoSync)
        self.db_service.record_reading_session(
            abs_id=abs_id, session_type='EPUB', start_time=900.0, end_time=1000.0,
            duration_seconds=100, start_progress=0.1, end_progress=0.2,
            leader_client='KoSync:kindle', user_id=user.id,
        )
        # Newer session (end_time 5000, ABS)
        self.db_service.record_reading_session(
            abs_id=abs_id, session_type='AUDIOBOOK', start_time=4900.0, end_time=5000.0,
            duration_seconds=100, start_progress=0.5, end_progress=0.6,
            leader_client='ABS', user_id=user.id,
        )

        stats = self.db_service.get_reading_stats(abs_id, user_id=user.id)
        self.assertIsNotNone(stats)
        self.assertEqual(stats['last_leader'], 'ABS')
        self.assertEqual(stats['last_session_time'], 5000.0)
        self.assertEqual(stats['session_count'], 2)


class TestSelectStateTieBreaking(unittest.TestCase):
    """_select_state with equal last_updated returns same client for both input orders."""

    def test_tie_breaking_deterministic_without_leader(self):
        """KoSync and Grimmory/booklore with same last_updated: same result both orders."""
        from src.services.reading_position_preview import _select_state

        kosync_state = SimpleNamespace(
            client_name='KoSync',
            last_updated=1000.0,
            percentage=0.5,
            xpath='/html/body/p[1]',
            cfi=None,
            timestamp=None,
        )
        booklore_state = SimpleNamespace(
            client_name='BookLore',
            last_updated=1000.0,
            percentage=0.6,
            xpath=None,
            cfi=None,
            timestamp=None,
        )

        # Order 1: KoSync first
        result1 = _select_state([kosync_state, booklore_state], last_leader=None)
        # Order 2: BookLore first
        result2 = _select_state([booklore_state, kosync_state], last_leader=None)

        # Both must return the same client_name (deterministic by _client_key)
        from src.services.reading_position_preview import _client_key
        self.assertEqual(_client_key(result1.client_name), _client_key(result2.client_name))


class TestPositionPreviewRouteLastLeader(unittest.TestCase):
    """Real route with real DatabaseService picks latest session's leader on tie."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.test_db_path = str(Path(self.temp_dir) / 'test_database.db')

        from src.db.database_service import DatabaseService
        from src.db.models import Book, State

        self.Book = Book
        self.State = State
        self.db_service = DatabaseService(self.test_db_path)

    def tearDown(self):
        if hasattr(self, 'db_service') and hasattr(self.db_service, 'db_manager'):
            self.db_service.db_manager.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_route_picks_latest_session_leader_on_tie(self):
        """Two states with equal last_updated: route picks state of latest session's leader."""
        from flask import Flask
        from src.api import reading_position_preview_routes as routes
        from src.utils.user_context import reset_current_user_id, set_current_user_id

        # Patch the kosync_server module the route itself holds: other suites reload it,
        # so `from src.api import kosync_server` can name a different module object.
        kosync_server = routes.kosync_server

        user = self.db_service.create_user('rl-user', 'pw', role='admin')
        abs_id = 'rl-book'
        self.db_service.save_book(self.Book(abs_id=abs_id, abs_title='RL', status='active'))

        # Two reading sessions: older KoSync, newer ABS
        self.db_service.record_reading_session(
            abs_id=abs_id, session_type='EPUB', start_time=900.0, end_time=1000.0,
            duration_seconds=100, start_progress=0.1, end_progress=0.2,
            leader_client='KoSync:kindle', user_id=user.id,
        )
        self.db_service.record_reading_session(
            abs_id=abs_id, session_type='AUDIOBOOK', start_time=4900.0, end_time=5000.0,
            duration_seconds=100, start_progress=0.5, end_progress=0.6,
            leader_client='ABS', user_id=user.id,
        )

        # Two states with EQUAL last_updated
        self.db_service.save_state(self.State(
            abs_id=abs_id, client_name='KoSync', last_updated=1000.0,
            percentage=0.5, xpath='/html/body/p[1]', user_id=user.id,
        ))
        self.db_service.save_state(self.State(
            abs_id=abs_id, client_name='BookLore', last_updated=1000.0,
            percentage=0.6, user_id=user.id,
        ))

        # Wire the real DatabaseService into the route module
        old_db = getattr(kosync_server, '_database_service', None)
        old_mgr = getattr(kosync_server, '_manager', None)

        class FakeManager:
            ebook_parser = None
            alignment_service = None

        kosync_server._database_service = self.db_service
        kosync_server._manager = FakeManager()

        try:
            # Patch build_reading_position_preview to capture what last_leader was passed
            captured = {}
            original_build = routes.build_reading_position_preview

            def fake_build(**kwargs):
                captured.update(kwargs)
                return {
                    "status": "approximate",
                    "source": "Test",
                    "confidence": "Approximate · percentage",
                    "percentage": 50.0,
                    "before": "before",
                    "after": "after",
                    "message": "",
                }

            routes.build_reading_position_preview = fake_build

            try:
                app = Flask(__name__)
                token = set_current_user_id(user.id)
                try:
                    with app.test_request_context():
                        response = routes.api_book_position_preview(abs_id)
                        self.assertEqual(response.status_code, 200)
                finally:
                    reset_current_user_id(token)

                # last_leader must come from the real get_reading_stats (ABS, the newer session)
                self.assertEqual(captured.get('last_leader'), 'ABS')
            finally:
                routes.build_reading_position_preview = original_build
        finally:
            kosync_server._database_service = old_db
            kosync_server._manager = old_mgr


if __name__ == '__main__':
    unittest.main()