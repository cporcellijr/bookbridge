"""Regression tests for KoSync GET equal-percentage fallback.

Verifies that _respond_from_book_states correctly handles:
- Locatorless synced state + equal-percentage sibling with locator → 200 with locator
- Behind sibling returns the newer synced state (furthest-wins gate)
- Ahead sibling returns furthest-wins
- No valid locator anywhere returns defensive 404
- No valid locator anywhere but 0% progress still returns the synced state
"""

import os
import shutil
import time
import hashlib
import unittest

# Set test environment BEFORE importing web_server
TEST_DIR = '/tmp/test_kosync_get'
os.environ['DATA_DIR'] = TEST_DIR
os.environ['KOSYNC_USER'] = 'testuser'
os.environ['KOSYNC_KEY'] = 'testpass'

if os.path.exists(TEST_DIR):
    shutil.rmtree(TEST_DIR)
os.makedirs(TEST_DIR, exist_ok=True)

from src.db.models import KosyncDocument, Book, State, ReadingSession, Setting, HardcoverDetails, UserCredential, KosyncUserProgress


class TestKosyncGetEqualPercentageFallback(unittest.TestCase):
    """Test the GET progress endpoint with equal-percentage sibling fallback."""

    @classmethod
    def setUpClass(cls):
        from src import web_server
        web_server.app, _ = web_server.create_app()
        cls.app = web_server.app
        cls.client = cls.app.test_client()

    @classmethod
    def tearDownClass(cls):
        # web_server.app is a shared module attribute other test files probe with
        # hasattr() to decide whether to build their own app; leaving it set here
        # leaks this class's app (and its bound database_service) into whichever
        # file collects next (see test_kosync_server.py setUpClass).
        from src import web_server
        if hasattr(web_server, 'app'):
            del web_server.app

    def setUp(self):
        """Clean tables and set up auth headers before each test."""
        from src import web_server
        from src.api import kosync_server

        db = web_server.database_service
        with db.get_session() as session:
            session.query(ReadingSession).delete()
            session.query(KosyncDocument).delete()
            session.query(KosyncUserProgress).delete()
            session.query(State).delete()
            session.query(Setting).delete()
            session.query(HardcoverDetails).delete()
            session.query(UserCredential).delete()
            session.query(Book).delete()

        if db.count_users() == 0:
            db.create_user("admin", "secret", role="admin")

        # Reset kosync server module state
        kosync_server._kosync_device_session_registry = None
        with kosync_server._booklore_shelf_mapping_cache_lock:
            kosync_server._booklore_shelf_mapping_cache.clear()
        with kosync_server._hardcover_list_mapping_cache_lock:
            kosync_server._hardcover_list_mapping_cache.clear()
        with kosync_server._kosync_open_sessions_lock:
            kosync_server._kosync_open_sessions.clear()
        with kosync_server._kosync_debounce_lock:
            kosync_server._kosync_debounce.clear()

        self.auth_headers = {
            'x-auth-user': 'testuser',
            'x-auth-key': hashlib.md5(b'testpass').hexdigest(),
            'Content-Type': 'application/json',
        }

    # ---- Helpers ----

    def _db(self):
        """Shortcut to module-level database_service."""
        from src import web_server
        return web_server.database_service

    def _setup_book_with_states(self, kosync_state_pct=1.0, kosync_xpath=None, kosync_cfi=None,
                                 sibling_pct=1.0, sibling_progress="/body/p[1]/text().0",
                                 sibling_exists=True):
        """Helper to create a book with KoSync State and optional sibling doc."""
        db = self._db()

        book = Book(abs_id="test-book-equal", abs_title="Test Book Equal", status="active")
        db.save_book(book)

        # KoSync State (the bridge-synced position)
        state = State(
            abs_id="test-book-equal",
            client_name="kosync",
            percentage=kosync_state_pct,
            timestamp=int(time.time()),
            last_updated=int(time.time()),
            xpath=kosync_xpath,
            cfi=kosync_cfi,
        )
        db.save_state(state)

        # Primary document hash for the GET endpoint
        primary_doc = KosyncDocument(
            document_hash="a" * 32,
            linked_abs_id="test-book-equal",
            percentage=kosync_state_pct,
            progress="",
            device="abs-kosync-bridge",
            device_id="abs-kosync-bridge",
        )
        db.save_kosync_document(primary_doc)

        # Optional sibling with valid locator
        if sibling_exists:
            sibling = KosyncDocument(
                document_hash="b" * 32,
                linked_abs_id="test-book-equal",
                percentage=sibling_pct,
                progress=sibling_progress,
                device="TestDevice",
                device_id="TEST123",
            )
            db.save_kosync_document(sibling)

        return book, primary_doc

    # ---- Test scenarios for equal-percentage fallback ----

    def test_equal_percentage_sibling_used(self):
        """Locatorless KoSync state + sibling at same % with locator → 200 with locator."""
        self._setup_book_with_states(
            kosync_state_pct=1.0,
            kosync_xpath=None,
            kosync_cfi=None,
            sibling_pct=1.0,
            sibling_progress="/body/DocFragment[17]/body/p[3]/text().0",
        )

        response = self.client.get(
            '/syncs/progress/' + ('a' * 32),
            headers=self.auth_headers,
        )

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        data = response.get_json()
        # Should return sibling's locator, not the locatorless synced state
        self.assertIn('progress', data)
        self.assertEqual(data['progress'], "/body/DocFragment[17]/body/p[3]/text().0")
        self.assertAlmostEqual(float(data['percentage']), 1.0)

    def test_equal_percentage_does_not_replace_valid_synced_locator(self):
        """Equal sibling fallback must not override an existing synced locator."""
        self._setup_book_with_states(
            kosync_state_pct=0.50,
            kosync_xpath="/body/authoritative/path",
            kosync_cfi=None,
            sibling_pct=0.50,
            sibling_progress="/body/stale/sibling",
        )

        response = self.client.get(
            '/syncs/progress/' + ('a' * 32),
            headers=self.auth_headers,
        )

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        data = response.get_json()
        self.assertEqual(data['progress'], "/body/authoritative/path")
        self.assertAlmostEqual(float(data['percentage']), 0.50)

    def test_behind_sibling_rejected(self):
        """Behind sibling returns the synced state, not the stale sibling."""
        self._setup_book_with_states(
            kosync_state_pct=0.75,
            kosync_xpath="/body/p[1]/text().0",
            kosync_cfi=None,
            sibling_pct=0.40,  # behind the synced state
            sibling_progress="/body/stale/path",
        )

        response = self.client.get(
            '/syncs/progress/' + ('a' * 32),
            headers=self.auth_headers,
        )

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        data = response.get_json()
        # Should return the synced state's progress/locator, not the stale sibling
        self.assertIn('progress', data)
        # The synced state has xpath, so it should be used
        self.assertEqual(data['progress'], "/body/p[1]/text().0")
        self.assertAlmostEqual(float(data['percentage']), 0.75)

    def test_ahead_sibling_furthest_wins(self):
        """Ahead sibling is returned (furthest-wins)."""
        self._setup_book_with_states(
            kosync_state_pct=0.50,
            kosync_xpath="/body/p[1]/text().0",
            kosync_cfi=None,
            sibling_pct=0.90,  # ahead of synced state
            sibling_progress="/body/ahead/path",
        )

        response = self.client.get(
            '/syncs/progress/' + ('a' * 32),
            headers=self.auth_headers,
        )

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        data = response.get_json()
        # Should return the ahead sibling's data
        self.assertEqual(data['progress'], "/body/ahead/path")
        self.assertAlmostEqual(float(data['percentage']), 0.90)

    def test_no_valid_locator_returns_404(self):
        """No valid locator anywhere returns defensive 404."""
        self._setup_book_with_states(
            kosync_state_pct=1.0,
            kosync_xpath=None,
            kosync_cfi=None,
            sibling_pct=1.0,
            sibling_progress="",  # empty progress → not a valid locator
            sibling_exists=True,
        )

        response = self.client.get(
            '/syncs/progress/' + ('a' * 32),
            headers=self.auth_headers,
        )

        # Should return 404 because no locator exists for positive progress
        self.assertEqual(response.status_code, 404, response.get_data(as_text=True))

    def test_no_sibling_exists_returns_synced_state(self):
        """When no sibling exists, the synced state is returned directly."""
        self._setup_book_with_states(
            kosync_state_pct=0.50,
            kosync_xpath="/body/p[1]/text().0",
            kosync_cfi=None,
            sibling_exists=False,
        )

        response = self.client.get(
            '/syncs/progress/' + ('a' * 32),
            headers=self.auth_headers,
        )

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        data = response.get_json()
        self.assertEqual(data['progress'], "/body/p[1]/text().0")
        self.assertAlmostEqual(float(data['percentage']), 0.50)

    def test_synced_state_has_no_locator_no_sibling_returns_404(self):
        """Synced state at 100% with no locator and no sibling → 404."""
        self._setup_book_with_states(
            kosync_state_pct=1.0,
            kosync_xpath=None,
            kosync_cfi=None,
            sibling_exists=False,
        )

        response = self.client.get(
            '/syncs/progress/' + ('a' * 32),
            headers=self.auth_headers,
        )

        self.assertEqual(response.status_code, 404, response.get_data(as_text=True))

    def test_zero_percent_synced_state_returns_synced_state(self):
        """Synced state at 0% returns synced state even without locator."""
        self._setup_book_with_states(
            kosync_state_pct=0.0,
            kosync_xpath=None,
            kosync_cfi=None,
            sibling_exists=False,
        )

        response = self.client.get(
            '/syncs/progress/' + ('a' * 32),
            headers=self.auth_headers,
        )

        # 0% returns 200 (no 404 because _suppress_empty_progress_response
        # only triggers for percentage > 0 with no locator)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        data = response.get_json()
        self.assertAlmostEqual(float(data['percentage']), 0.0)

    def test_multiple_siblings_picks_highest_percentage(self):
        """Multiple siblings: the one with highest percentage is used."""
        db = self._db()

        book = Book(abs_id="test-book-multi", abs_title="Test Book Multi", status="active")
        db.save_book(book)

        # KoSync State at 30%
        state = State(
            abs_id="test-book-multi",
            client_name="kosync",
            percentage=0.30,
            timestamp=int(time.time()),
            last_updated=int(time.time()),
        )
        db.save_state(state)

        # Primary doc
        primary = KosyncDocument(
            document_hash="c" * 32,
            linked_abs_id="test-book-multi",
            percentage=0.30,
            progress="",
        )
        db.save_kosync_document(primary)

        # Three siblings at different percentages
        siblings = [
            KosyncDocument(document_hash="d" * 32, linked_abs_id="test-book-multi",
                           percentage=0.10, progress="/body/10"),
            KosyncDocument(document_hash="e" * 32, linked_abs_id="test-book-multi",
                           percentage=0.60, progress="/body/60"),
            KosyncDocument(document_hash="f" * 32, linked_abs_id="test-book-multi",
                           percentage=0.90, progress="/body/90"),
        ]
        for sib in siblings:
            db.save_kosync_document(sib)

        response = self.client.get(
            '/syncs/progress/' + ('c' * 32),
            headers=self.auth_headers,
        )

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        data = response.get_json()
        # Highest sibling (90%) should win since it's ahead of synced state (30%)
        self.assertEqual(data['progress'], "/body/90")
        self.assertAlmostEqual(float(data['percentage']), 0.90)

    def test_synced_state_has_cfi_used_as_progress(self):
        """Synced state with cfi but no xpath uses cfi as progress."""
        self._setup_book_with_states(
            kosync_state_pct=0.50,
            kosync_xpath=None,  # no xpath
            kosync_cfi="/6/4[chap1]!",  # but has cfi
            sibling_exists=False,
        )

        response = self.client.get(
            '/syncs/progress/' + ('a' * 32),
            headers=self.auth_headers,
        )

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        data = response.get_json()
        # Should return CFI as progress since xpath is empty
        self.assertEqual(data['progress'], "/6/4[chap1]!")
        self.assertAlmostEqual(float(data['percentage']), 0.50)
