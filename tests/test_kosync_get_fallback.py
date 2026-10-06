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
from unittest.mock import MagicMock, patch

# Set test environment BEFORE importing web_server
TEST_DIR = '/tmp/test_kosync_get'
os.environ['DATA_DIR'] = TEST_DIR
os.environ['KOSYNC_USER'] = 'testuser'
os.environ['KOSYNC_KEY'] = 'testpass'

if os.path.exists(TEST_DIR):
    shutil.rmtree(TEST_DIR)
os.makedirs(TEST_DIR, exist_ok=True)

from src.db.models import KosyncDocument, Book, State, ReadingSession, Setting, HardcoverDetails, UserCredential, KosyncUserProgress
from src.api import kosync_server


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
        # Clear XPath index cache to prevent test pollution (failure mode #17)
        with kosync_server._XPATH_INDEX_CACHE_LOCK:
            kosync_server._XPATH_INDEX_CACHE.clear()

        # The same-document XPath comparison ships OFF; the tests that exercise
        # it turn it on here and tearDown restores the process default.
        self._prev_xpath_order = os.environ.get("KOSYNC_XPATH_ORDER_ENABLED")
        os.environ["KOSYNC_XPATH_ORDER_ENABLED"] = "true"

        self.auth_headers = {
            'x-auth-user': 'testuser',
            'x-auth-key': hashlib.md5(b'testpass').hexdigest(),
            'Content-Type': 'application/json',
        }

    def tearDown(self):
        """Restore the process-wide setting so the suite stays order-independent."""
        if self._prev_xpath_order is None:
            os.environ.pop("KOSYNC_XPATH_ORDER_ENABLED", None)
        else:
            os.environ["KOSYNC_XPATH_ORDER_ENABLED"] = self._prev_xpath_order

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

        book = Book(
            abs_id="test-book-equal",
            abs_title="Test Book Equal",
            kosync_doc_id="a" * 32,
            status="active",
            ebook_filename="test.epub",
        )
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

    def test_same_hash_xpath_order_overrides_misleading_percentage(self):
        """Same EPUB: resolved XPath order wins when raw percentages disagree."""
        from src.api import kosync_server

        synced_xpath = "/body/DocFragment[20]/body/p[20]/text().0"
        device_xpath = "/body/DocFragment[20]/body/p[15]/text().0"

        _, primary_doc = self._setup_book_with_states(
            kosync_state_pct=0.23,
            kosync_xpath=synced_xpath,
            sibling_exists=False,
        )

        # Reproduce the bug: the stale KOReader locator has a numerically higher
        # percentage even though it resolves to an earlier text position.
        primary_doc.percentage = 0.24
        primary_doc.progress = device_xpath
        primary_doc.filename = "test.epub"
        self._db().save_kosync_document(primary_doc)

        parser = MagicMock()
        parser.resolve_xpath_to_index.side_effect = lambda _filename, xpath: {
            device_xpath: 100,
            synced_xpath: 200,
        }[xpath]

        container = MagicMock()
        container.ebook_parser.return_value = parser

        with patch.object(kosync_server, "_container", container):
            response = self.client.get(
                "/syncs/progress/" + ("a" * 32),
                headers=self.auth_headers,
            )

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        data = response.get_json()
        self.assertEqual(data["progress"], synced_xpath)
        self.assertAlmostEqual(float(data["percentage"]), 0.23)
        self.assertEqual(parser.resolve_xpath_to_index.call_count, 2)
        parser.resolve_xpath_to_index.assert_any_call("test.epub", device_xpath)
        parser.resolve_xpath_to_index.assert_any_call("test.epub", synced_xpath)

    def test_cbz_skips_xpath_order_and_keeps_percentage_fallback(self):
        """CBZ pages and stale EPUB locators must never trigger EPUB parsing."""
        for filename, synced_progress, device_progress in (
            ("comic.cbz", "15", "16"),
            ("comic.CBZ", "/body/DocFragment[20]/body/p[20]/text().0",
             "/body/DocFragment[20]/body/p[15]/text().0"),
        ):
            with self.subTest(filename=filename):
                book, primary_doc = self._setup_book_with_states(
                    kosync_state_pct=0.23,
                    kosync_xpath=synced_progress,
                    sibling_exists=False,
                )
                book.ebook_filename = filename
                self._db().save_book(book)
                primary_doc.percentage = 0.24
                primary_doc.progress = device_progress
                primary_doc.filename = filename
                self._db().save_kosync_document(primary_doc)

                parser = MagicMock()
                parser.resolve_xpath_to_index.side_effect = [100, 200]
                container = MagicMock()
                container.ebook_parser.return_value = parser

                with patch.object(kosync_server, "_container", container):
                    response = self.client.get(
                        "/syncs/progress/" + ("a" * 32),
                        headers=self.auth_headers,
                    )

                self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
                data = response.get_json()
                self.assertEqual(data["progress"], device_progress)
                self.assertAlmostEqual(float(data["percentage"]), 0.24)
                parser.resolve_xpath_to_index.assert_not_called()

    def test_same_hash_unresolved_xpath_keeps_percentage_fallback(self):
        """Unresolvable XPaths preserve the existing percentage comparison."""
        from src.api import kosync_server

        synced_xpath = "/body/DocFragment[20]/body/p[20]/text().0"
        device_xpath = "/body/DocFragment[20]/body/p[15]/text().0"

        _, primary_doc = self._setup_book_with_states(
            kosync_state_pct=0.23,
            kosync_xpath=synced_xpath,
            sibling_exists=False,
        )

        primary_doc.percentage = 0.24
        primary_doc.progress = device_xpath
        primary_doc.filename = "test.epub"
        self._db().save_kosync_document(primary_doc)

        parser = MagicMock()
        parser.resolve_xpath_to_index.return_value = None

        container = MagicMock()
        container.ebook_parser.return_value = parser

        with patch.object(kosync_server, "_container", container):
            response = self.client.get(
                "/syncs/progress/" + ("a" * 32),
                headers=self.auth_headers,
            )

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        data = response.get_json()
        self.assertEqual(data["progress"], device_xpath)
        self.assertAlmostEqual(float(data["percentage"]), 0.24)

    def test_sibling_hash_keeps_percentage_fallback(self):
        """A sibling EPUB must not resolve the primary State XPath against its file."""
        from src.api import kosync_server

        synced_xpath = "/body/DocFragment[20]/body/p[20]/text().0"
        sibling_xpath = "/body/DocFragment[20]/body/p[15]/text().0"

        self._setup_book_with_states(
            kosync_state_pct=0.23,
            kosync_xpath=synced_xpath,
            sibling_pct=0.24,
            sibling_progress=sibling_xpath,
        )

        sibling_doc = self._db().get_kosync_document("b" * 32)
        sibling_doc.filename = "sibling.epub"
        self._db().save_kosync_document(sibling_doc)

        # If the primary State XPath were incorrectly compared against this
        # sibling EPUB, these positions would make the synced State appear ahead.
        parser = MagicMock()
        parser.resolve_xpath_to_index.side_effect = lambda _filename, xpath: {
            sibling_xpath: 100,
            synced_xpath: 200,
        }[xpath]

        container = MagicMock()
        container.ebook_parser.return_value = parser

        with patch.object(kosync_server, "_container", container):
            response = self.client.get(
                "/syncs/progress/" + ("b" * 32),
                headers=self.auth_headers,
            )

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        data = response.get_json()
        self.assertEqual(data["progress"], sibling_xpath)
        self.assertAlmostEqual(float(data["percentage"]), 0.24)
        parser.resolve_xpath_to_index.assert_not_called()

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

    # ---- New tests for hardened XPath ordering ----

    def test_pct_bound_exceeded_skips_comparison(self):
        """Percentages far apart: bound exceeded, percentage alone decides, resolver not called."""
        from src.api import kosync_server

        synced_xpath = "/body/DocFragment[20]/body/p[20]/text().0"
        device_xpath = "/body/DocFragment[20]/body/p[15]/text().0"

        _, primary_doc = self._setup_book_with_states(
            kosync_state_pct=0.23,
            kosync_xpath=synced_xpath,
            sibling_exists=False,
        )

        primary_doc.percentage = 0.80  # Far from 0.23, outside 0.10 bound
        primary_doc.progress = device_xpath
        primary_doc.filename = "test.epub"
        self._db().save_kosync_document(primary_doc)

        parser = MagicMock()
        parser.resolve_xpath_to_index.side_effect = lambda _filename, xpath: {
            device_xpath: 100,
            synced_xpath: 200,
        }[xpath]

        container = MagicMock()
        container.ebook_parser.return_value = parser

        with patch.object(kosync_server, "_container", container):
            response = self.client.get(
                "/syncs/progress/" + ("a" * 32),
                headers=self.auth_headers,
            )

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        data = response.get_json()
        # Device percentage (0.80) > synced (0.23) and outside bound → device wins
        self.assertEqual(data["progress"], device_xpath)
        self.assertAlmostEqual(float(data["percentage"]), 0.80)
        parser.resolve_xpath_to_index.assert_not_called()

    def test_pct_bound_configurable(self):
        """With env var wide enough, resolver is called and XPath order decides."""
        from src.api import kosync_server

        synced_xpath = "/body/DocFragment[20]/body/p[20]/text().0"
        device_xpath = "/body/DocFragment[20]/body/p[15]/text().0"

        _, primary_doc = self._setup_book_with_states(
            kosync_state_pct=0.23,
            kosync_xpath=synced_xpath,
            sibling_exists=False,
        )

        primary_doc.percentage = 0.80  # Far from 0.23
        primary_doc.progress = device_xpath
        primary_doc.filename = "test.epub"
        self._db().save_kosync_document(primary_doc)

        parser = MagicMock()
        parser.resolve_xpath_to_index.side_effect = lambda _filename, xpath: {
            device_xpath: 100,
            synced_xpath: 200,
        }[xpath]

        container = MagicMock()
        container.ebook_parser.return_value = parser

        os.environ["KOSYNC_XPATH_ORDER_MAX_PCT_DELTA"] = "0.90"
        try:
            with patch.object(kosync_server, "_container", container):
                response = self.client.get(
                    "/syncs/progress/" + ("a" * 32),
                    headers=self.auth_headers,
                )
        finally:
            os.environ.pop("KOSYNC_XPATH_ORDER_MAX_PCT_DELTA", None)

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        data = response.get_json()
        # Within bound now, XPath order: synced (200) > device (100) → synced wins
        self.assertEqual(data["progress"], synced_xpath)
        self.assertAlmostEqual(float(data["percentage"]), 0.23)
        self.assertEqual(parser.resolve_xpath_to_index.call_count, 2)

    def test_filename_mismatch_skips_comparison(self):
        """Document filename doesn't match book's ebook_filename or original_ebook_filename."""
        from src.api import kosync_server

        synced_xpath = "/body/DocFragment[20]/body/p[20]/text().0"
        device_xpath = "/body/DocFragment[20]/body/p[15]/text().0"

        _, primary_doc = self._setup_book_with_states(
            kosync_state_pct=0.23,
            kosync_xpath=synced_xpath,
            sibling_exists=False,
        )

        primary_doc.percentage = 0.24  # Inside 0.10 bound
        primary_doc.progress = device_xpath
        primary_doc.filename = "other-build.epub"  # Mismatch
        self._db().save_kosync_document(primary_doc)

        parser = MagicMock()
        parser.resolve_xpath_to_index.side_effect = lambda _filename, xpath: {
            device_xpath: 100,
            synced_xpath: 200,
        }[xpath]

        container = MagicMock()
        container.ebook_parser.return_value = parser

        with patch.object(kosync_server, "_container", container):
            response = self.client.get(
                "/syncs/progress/" + ("a" * 32),
                headers=self.auth_headers,
            )

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        data = response.get_json()
        # Filename mismatch → percentage decides, device wins (0.24 > 0.23)
        self.assertEqual(data["progress"], device_xpath)
        self.assertAlmostEqual(float(data["percentage"]), 0.24)
        parser.resolve_xpath_to_index.assert_not_called()

    def test_original_ebook_filename_satisfies_check(self):
        """Book's original_ebook_filename matches document filename → resolver called."""
        from src.api import kosync_server

        synced_xpath = "/body/DocFragment[20]/body/p[20]/text().0"
        device_xpath = "/body/DocFragment[20]/body/p[15]/text().0"

        _, primary_doc = self._setup_book_with_states(
            kosync_state_pct=0.23,
            kosync_xpath=synced_xpath,
            sibling_exists=False,
        )

        # Change book's ebook_filename but set original_ebook_filename to match
        db = self._db()
        book = db.get_book("test-book-equal")
        book.ebook_filename = "different.epub"
        book.original_ebook_filename = "test.epub"
        db.save_book(book)

        primary_doc.percentage = 0.24  # Inside 0.10 bound
        primary_doc.progress = device_xpath
        primary_doc.filename = "test.epub"  # Matches original_ebook_filename
        self._db().save_kosync_document(primary_doc)

        parser = MagicMock()
        parser.resolve_xpath_to_index.side_effect = lambda _filename, xpath: {
            device_xpath: 100,
            synced_xpath: 200,
        }[xpath]

        container = MagicMock()
        container.ebook_parser.return_value = parser

        with patch.object(kosync_server, "_container", container):
            response = self.client.get(
                "/syncs/progress/" + ("a" * 32),
                headers=self.auth_headers,
            )

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        data = response.get_json()
        # Filename matches original_ebook_filename → XPath order: synced wins
        self.assertEqual(data["progress"], synced_xpath)
        self.assertAlmostEqual(float(data["percentage"]), 0.23)
        self.assertEqual(parser.resolve_xpath_to_index.call_count, 2)

    def test_repeated_gets_do_not_re_resolve(self):
        """Second identical GET uses cache, does not re-parse."""
        from src.api import kosync_server

        synced_xpath = "/body/DocFragment[20]/body/p[20]/text().0"
        device_xpath = "/body/DocFragment[20]/body/p[15]/text().0"

        _, primary_doc = self._setup_book_with_states(
            kosync_state_pct=0.23,
            kosync_xpath=synced_xpath,
            sibling_exists=False,
        )

        primary_doc.percentage = 0.24  # Inside bound
        primary_doc.progress = device_xpath
        primary_doc.filename = "test.epub"
        self._db().save_kosync_document(primary_doc)

        parser = MagicMock()
        parser.resolve_xpath_to_index.side_effect = lambda _filename, xpath: {
            device_xpath: 100,
            synced_xpath: 200,
        }[xpath]

        container = MagicMock()
        container.ebook_parser.return_value = parser

        with patch.object(kosync_server, "_container", container):
            # First request
            response1 = self.client.get(
                "/syncs/progress/" + ("a" * 32),
                headers=self.auth_headers,
            )
            # Second identical request
            response2 = self.client.get(
                "/syncs/progress/" + ("a" * 32),
                headers=self.auth_headers,
            )

        self.assertEqual(response1.status_code, 200)
        self.assertEqual(response2.status_code, 200)
        data1 = response1.get_json()
        data2 = response2.get_json()
        # Both should return same result (synced wins via XPath order)
        self.assertEqual(data1["progress"], synced_xpath)
        self.assertEqual(data2["progress"], synced_xpath)
        # First request resolves both XPaths (2 calls), second uses cache (0 additional)
        self.assertEqual(parser.resolve_xpath_to_index.call_count, 2)

    def test_unresolvable_pairs_cached_too(self):
        """Parser returns None for both XPaths → cached as (None, None), no re-parse on second GET."""
        from src.api import kosync_server

        synced_xpath = "/body/DocFragment[20]/body/p[20]/text().0"
        device_xpath = "/body/DocFragment[20]/body/p[15]/text().0"

        _, primary_doc = self._setup_book_with_states(
            kosync_state_pct=0.23,
            kosync_xpath=synced_xpath,
            sibling_exists=False,
        )

        primary_doc.percentage = 0.24  # Inside bound
        primary_doc.progress = device_xpath
        primary_doc.filename = "test.epub"
        self._db().save_kosync_document(primary_doc)

        parser = MagicMock()
        parser.resolve_xpath_to_index.return_value = None

        container = MagicMock()
        container.ebook_parser.return_value = parser

        with patch.object(kosync_server, "_container", container):
            # First request
            response1 = self.client.get(
                "/syncs/progress/" + ("a" * 32),
                headers=self.auth_headers,
            )
            # Second identical request
            response2 = self.client.get(
                "/syncs/progress/" + ("a" * 32),
                headers=self.auth_headers,
            )

        self.assertEqual(response1.status_code, 200)
        self.assertEqual(response2.status_code, 200)
        data1 = response1.get_json()
        data2 = response2.get_json()
        # Both fall back to device percentage
        self.assertEqual(data1["progress"], device_xpath)
        self.assertEqual(data2["progress"], device_xpath)
        self.assertAlmostEqual(float(data1["percentage"]), 0.24)
        self.assertAlmostEqual(float(data2["percentage"]), 0.24)
        # First request resolves both XPaths (2 calls), second uses cache (0 additional)
        self.assertEqual(parser.resolve_xpath_to_index.call_count, 2)

    def test_override_logged_at_info(self):
        """Contradicting order emits INFO log with canonical XPath order message."""
        from src.api import kosync_server

        synced_xpath = "/body/DocFragment[20]/body/p[20]/text().0"
        device_xpath = "/body/DocFragment[20]/body/p[15]/text().0"

        _, primary_doc = self._setup_book_with_states(
            kosync_state_pct=0.23,
            kosync_xpath=synced_xpath,
            sibling_exists=False,
        )

        primary_doc.percentage = 0.24  # Inside bound
        primary_doc.progress = device_xpath
        primary_doc.filename = "test.epub"
        self._db().save_kosync_document(primary_doc)

        parser = MagicMock()
        parser.resolve_xpath_to_index.side_effect = lambda _filename, xpath: {
            device_xpath: 100,
            synced_xpath: 200,
        }[xpath]

        container = MagicMock()
        container.ebook_parser.return_value = parser

        with patch.object(kosync_server, "_container", container):
            with self.assertLogs('src.api.kosync_server', level='INFO') as cm:
                response = self.client.get(
                    "/syncs/progress/" + ("a" * 32),
                    headers=self.auth_headers,
                )

        self.assertEqual(response.status_code, 200)
        # Check that INFO log contains the override message
        log_output = '\n'.join(cm.output)
        self.assertIn("canonical XPath order overrides percentage", log_output)

    def test_per_user_progress_row_same_path(self):
        """A per-user KosyncUserProgress row takes the same XPath-ordering path.

        PR #380's other tests all exercise the legacy shared-KosyncDocument
        fallback; multi-user installs read KosyncUserProgress instead."""
        from src.api import kosync_server

        synced_xpath = "/body/DocFragment[20]/body/p[20]/text().0"
        device_xpath = "/body/DocFragment[20]/body/p[15]/text().0"
        decoy_xpath = "/body/DocFragment[40]/body/p[1]/text().0"

        _, primary_doc = self._setup_book_with_states(
            kosync_state_pct=0.23,
            kosync_xpath=synced_xpath,
            sibling_exists=False,
        )

        # Decoy on the SHARED row: if the per-user row were not the one read,
        # this would win outright on percentage and the assertions below fail.
        primary_doc.percentage = 0.90
        primary_doc.progress = decoy_xpath
        primary_doc.filename = "test.epub"
        db = self._db()
        db.save_kosync_document(primary_doc)

        # user_id=None resolves the same default user the request authenticates as.
        user_row = db.upsert_user_kosync_progress(
            "a" * 32,
            0.24,
            progress=device_xpath,
            device="TestDevice",
            device_id="TEST123",
            user_id=None,
        )
        self.assertIsNotNone(user_row, "per-user progress row was not created")

        parser = MagicMock()
        parser.resolve_xpath_to_index.side_effect = lambda _filename, xpath: {
            device_xpath: 100,
            synced_xpath: 200,
        }[xpath]

        container = MagicMock()
        container.ebook_parser.return_value = parser

        with patch.object(kosync_server, "_container", container):
            response = self.client.get(
                "/syncs/progress/" + ("a" * 32),
                headers=self.auth_headers,
            )

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        data = response.get_json()
        # Resolved order (synced 200 > device 100) beats the device's higher pct.
        self.assertEqual(data["progress"], synced_xpath)
        self.assertAlmostEqual(float(data["percentage"]), 0.23)
        # Only the per-user row's xpath was resolved -- never the decoy.
        self.assertEqual(parser.resolve_xpath_to_index.call_count, 2)
        resolved = {c.args[1] for c in parser.resolve_xpath_to_index.call_args_list}
        self.assertEqual(resolved, {device_xpath, synced_xpath})


    def test_xpath_order_disabled_by_default_skips_comparison(self):
        """With the setting absent, the comparison never runs (shipped default)."""
        from src.api import kosync_server

        synced_xpath = "/body/DocFragment[20]/body/p[20]/text().0"
        device_xpath = "/body/DocFragment[20]/body/p[15]/text().0"

        _, primary_doc = self._setup_book_with_states(
            kosync_state_pct=0.23,
            kosync_xpath=synced_xpath,
            sibling_exists=False,
        )
        primary_doc.percentage = 0.24
        primary_doc.progress = device_xpath
        primary_doc.filename = "test.epub"
        self._db().save_kosync_document(primary_doc)

        parser = MagicMock()
        parser.resolve_xpath_to_index.side_effect = lambda _f, xpath: {
            device_xpath: 100, synced_xpath: 200,
        }[xpath]
        container = MagicMock()
        container.ebook_parser.return_value = parser

        os.environ.pop("KOSYNC_XPATH_ORDER_ENABLED", None)
        with patch.object(kosync_server, "_container", container):
            response = self.client.get(
                "/syncs/progress/" + ("a" * 32), headers=self.auth_headers,
            )

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        data = response.get_json()
        # Percentage alone decides: the device's 0.24 beats the synced 0.23.
        self.assertEqual(data["progress"], device_xpath)
        self.assertAlmostEqual(float(data["percentage"]), 0.24)
        parser.resolve_xpath_to_index.assert_not_called()

    def test_xpath_order_accepts_on_spelling(self):
        """A checkbox POSTs "on", not "true" -- both must enable the gate."""
        from src.api import kosync_server

        synced_xpath = "/body/DocFragment[20]/body/p[20]/text().0"
        device_xpath = "/body/DocFragment[20]/body/p[15]/text().0"

        for spelling in ("true", "on"):
            with self.subTest(spelling=spelling):
                self.setUp()
                _, primary_doc = self._setup_book_with_states(
                    kosync_state_pct=0.23,
                    kosync_xpath=synced_xpath,
                    sibling_exists=False,
                )
                primary_doc.percentage = 0.24
                primary_doc.progress = device_xpath
                primary_doc.filename = "test.epub"
                self._db().save_kosync_document(primary_doc)

                parser = MagicMock()
                parser.resolve_xpath_to_index.side_effect = lambda _f, xpath: {
                    device_xpath: 100, synced_xpath: 200,
                }[xpath]
                container = MagicMock()
                container.ebook_parser.return_value = parser

                os.environ["KOSYNC_XPATH_ORDER_ENABLED"] = spelling
                with patch.object(kosync_server, "_container", container):
                    response = self.client.get(
                        "/syncs/progress/" + ("a" * 32), headers=self.auth_headers,
                    )

                data = response.get_json()
                self.assertEqual(data["progress"], synced_xpath)
                self.assertEqual(parser.resolve_xpath_to_index.call_count, 2)
