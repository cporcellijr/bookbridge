"""Tests for the dashboard search hand-off row and the Add Book queue badge.

The dashboard search box is a client-side *filter* over books the user already
syncs — searching for a title that isn't matched yet used to hide every card
and leave a blank page with no explanation. The dashboard now renders a
hand-off row that links into Add Book's library search (``/add-book?search=``).

Separately, the batch-match queue is persistent server-side state, so a user
who queued books and navigated away had no visible route back to it. The
"Add Book" nav tab now carries a count badge whenever that user's queue is
non-empty.

These tests cover the server-rendered halves of both: the hand-off markup and
the JS hand-off URL are present on the dashboard, the badge appears only when
the queue has items, and a queue read that blows up degrades to no badge
rather than a 500.
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_webserver import MockContainer
from src.db.database_service import DatabaseService
from src.db.models import Book

_TEMPLATES = str(Path(__file__).parent.parent / "templates")


class TestDashboardSearchHandoff(unittest.TestCase):
    """Dashboard hand-off row + Add Book nav queue badge."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ['DATA_DIR'] = self.tmp
        os.environ['BOOKS_DIR'] = self.tmp
        # index.html extends base.html, so the Jinja loader must point at the
        # real templates directory (create_app defaults to /app/templates).
        self._orig_template_dir = os.environ.get('TEMPLATE_DIR')
        os.environ['TEMPLATE_DIR'] = _TEMPLATES

        self.svc = DatabaseService(os.path.join(self.tmp, "handoff.db"))
        self.user = self.svc.create_user("handoff-user", "handoffpw", role="admin")
        book = Book(
            abs_id="book-handoff", abs_title="Handoff Test Book",
            ebook_filename="book-handoff.epub", status="active", duration=100,
            user_id=self.user.id,
        )
        self.svc.save_book(book)
        self.svc.link_user_book(self.user.id, "book-handoff")

        self.mock_container = MockContainer()
        self.mock_container.mock_database_service = self.svc

        import src.db.migration_utils
        self._orig_init = src.db.migration_utils.initialize_database
        src.db.migration_utils.initialize_database = lambda data_dir: self.svc

        from src.web_server import create_app
        self.app, _ = create_app(test_container=self.mock_container)
        self.app.config['TESTING'] = True
        self.app.config['LOGIN_DISABLED'] = False
        self.client = self.app.test_client()

    def tearDown(self):
        import src.db.migration_utils
        src.db.migration_utils.initialize_database = self._orig_init
        if self._orig_template_dir is None:
            os.environ.pop('TEMPLATE_DIR', None)
        else:
            os.environ['TEMPLATE_DIR'] = self._orig_template_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _login(self):
        resp = self.client.post(
            '/login', data={'username': "handoff-user", 'password': "handoffpw"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 302, "login failed")

    def _dashboard_html(self, queue=()):
        """GET / with the acting user's match queue stubbed to ``queue``."""
        with patch('src.web_server._load_match_queue', return_value=list(queue)):
            resp = self.client.get('/')
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    # ---- hand-off row ----------------------------------------------------

    def test_dashboard_renders_search_handoff_row(self):
        """The hidden hand-off row and its link element ship with the page."""
        self._login()
        html = self._dashboard_html()
        self.assertIn('id="search-handoff"', html)
        self.assertIn('id="search-handoff-msg"', html)
        self.assertIn('id="search-handoff-link"', html)

    def test_handoff_row_starts_hidden(self):
        """It must not show until the user actually types a query."""
        self._login()
        html = self._dashboard_html()
        self.assertIn('class="search-handoff hidden"', html)

    def test_dashboard_exposes_search_tab_url(self):
        """The JS builds its href from the Suggestions Search tab's real URL."""
        self._login()
        html = self._dashboard_html()
        self.assertIn("/suggestions?tab=search&q=", html)
        self.assertNotIn("/add-book", html)

    # ---- Add Book queue badge -------------------------------------------

    def test_no_badge_when_queue_is_empty(self):
        self._login()
        html = self._dashboard_html(queue=[])
        self.assertNotIn('nav-tab-badge', html)

    def test_badge_shows_queue_count(self):
        self._login()
        html = self._dashboard_html(queue=[{'abs_id': 'a'}, {'abs_id': 'b'}, {'abs_id': 'c'}])
        self.assertIn('<span class="nav-tab-badge">3</span>', html)

    def test_badge_read_failure_does_not_break_the_page(self):
        """A queue read that raises degrades to no badge, never a 500."""
        self._login()
        with patch('src.web_server._load_match_queue', side_effect=RuntimeError("boom")):
            resp = self.client.get('/')
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn('nav-tab-badge', resp.get_data(as_text=True))


if __name__ == '__main__':
    unittest.main()
