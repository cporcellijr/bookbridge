"""
Issue #412 — the dashboard's 30-second refresh must not rebuild the world.

``templates/index.html`` polls every 30 seconds and redraws six fields, but the
endpoint it called rebuilt the entire dashboard payload for every visible book.
The expensive part is the drift badge: an audio client's position is converted
onto the text axis through the book's alignment map, a 10-15MB JSON blob read
against a 3-entry cache. On a 150-book library that is 150 map loads and parses
every 30 seconds — one core pegged, on a loop, for a number the refresh never
reads.

Three properties are covered here:

  (a) the drift computation bails before any alignment work when fewer than two
      clients report a position;
  (b) an unchanged book's drift is not recomputed on the next render;
  (c) the refresh endpoint serves its fields from the database alone and never
      enters the full dashboard build.
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import src.web_server as ws
from tests.test_webserver import MockContainer
from src.db.database_service import DatabaseService
from src.db.models import Book, State

_TEMPLATES = str(Path(__file__).parent.parent / "templates")


def _mapping(states, abs_id="abs-1", duration=10000):
    return {
        "abs_id": abs_id,
        "duration": duration,
        "sync_mode": "audiobook",
        "states": states,
    }


class TestSyncWarningAlignmentCost(unittest.TestCase):
    """The drift number must cost nothing when it cannot exist."""

    def setUp(self):
        ws._DASHBOARD_SYNC_WARNING_CACHE.clear()
        self.alignment = MagicMock()
        self.alignment.get_progress_for_time.return_value = 0.40
        self._manager_patch = patch.object(
            ws, "manager", SimpleNamespace(alignment_service=self.alignment)
        )
        self._manager_patch.start()
        self.addCleanup(self._manager_patch.stop)
        self.addCleanup(ws._DASHBOARD_SYNC_WARNING_CACHE.clear)

    def test_single_reporting_client_never_touches_the_alignment_map(self):
        """The reported configuration: Audiobookshelf only. One client cannot
        drift from anything, so converting its position is pure waste."""
        warning = ws._compute_dashboard_sync_warning_pct(
            _mapping({"abs": {"percentage": 42.0, "timestamp": 4200}}),
            {"abs": True},
        )

        self.assertEqual(warning, 0.0)
        self.alignment.get_progress_for_time.assert_not_called()

    def test_zero_progress_peer_does_not_make_it_comparable(self):
        """A client at 0% is filtered out of the comparison, so an audio client
        beside it is still the only candidate."""
        warning = ws._compute_dashboard_sync_warning_pct(
            _mapping({
                "abs": {"percentage": 42.0, "timestamp": 4200},
                "kosync": {"percentage": 0.0, "timestamp": 0},
            }),
            {"abs": True, "kosync": True},
        )

        self.assertEqual(warning, 0.0)
        self.alignment.get_progress_for_time.assert_not_called()

    def test_two_clients_still_convert_and_report_drift(self):
        """The short-circuit must not cost the feature its actual job."""
        warning = ws._compute_dashboard_sync_warning_pct(
            _mapping({
                "abs": {"percentage": 55.0, "timestamp": 5500},
                "kosync": {"percentage": 30.0, "timestamp": 0},
            }),
            {"abs": True, "kosync": True},
        )

        # ABS converts to 40% via the alignment map; KoSync reads 30% directly.
        self.assertEqual(warning, 10.0)
        self.alignment.get_progress_for_time.assert_called_once()


class TestSyncWarningMemoization(unittest.TestCase):
    """A position that has not moved cannot have drifted differently."""

    def setUp(self):
        ws._DASHBOARD_SYNC_WARNING_CACHE.clear()
        self.alignment = MagicMock()
        self.alignment.get_progress_for_time.return_value = 0.40
        self._manager_patch = patch.object(
            ws, "manager", SimpleNamespace(alignment_service=self.alignment)
        )
        self._manager_patch.start()
        self.addCleanup(self._manager_patch.stop)
        self.addCleanup(ws._DASHBOARD_SYNC_WARNING_CACHE.clear)

    @staticmethod
    def _states(abs_pct=55.0, abs_ts=5500):
        return {
            "abs": {"percentage": abs_pct, "timestamp": abs_ts},
            "kosync": {"percentage": 30.0, "timestamp": 0},
        }

    def test_unchanged_book_is_not_recomputed(self):
        integrations = {"abs": True, "kosync": True}
        first = ws._compute_dashboard_sync_warning_pct(_mapping(self._states()), integrations)
        second = ws._compute_dashboard_sync_warning_pct(_mapping(self._states()), integrations)

        self.assertEqual(first, second)
        self.assertEqual(
            self.alignment.get_progress_for_time.call_count, 1,
            "an unchanged book re-loaded its alignment map on the next render",
        )

    def test_moved_position_is_recomputed(self):
        integrations = {"abs": True, "kosync": True}
        ws._compute_dashboard_sync_warning_pct(_mapping(self._states()), integrations)
        self.alignment.get_progress_for_time.return_value = 0.60
        moved = ws._compute_dashboard_sync_warning_pct(
            _mapping(self._states(abs_pct=70.0, abs_ts=7000)), integrations
        )

        self.assertEqual(moved, 30.0)
        self.assertEqual(self.alignment.get_progress_for_time.call_count, 2)

    def test_cache_is_keyed_per_book(self):
        integrations = {"abs": True, "kosync": True}
        ws._compute_dashboard_sync_warning_pct(
            _mapping(self._states(), abs_id="abs-1"), integrations
        )
        ws._compute_dashboard_sync_warning_pct(
            _mapping(self._states(), abs_id="abs-2"), integrations
        )

        self.assertEqual(self.alignment.get_progress_for_time.call_count, 2)


class TestCompactRefreshEndpoint(unittest.TestCase):
    """The periodic refresh serves its six fields without the dashboard build."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ['DATA_DIR'] = self.tmp
        os.environ['BOOKS_DIR'] = self.tmp
        self._orig_template_dir = os.environ.get('TEMPLATE_DIR')
        os.environ['TEMPLATE_DIR'] = _TEMPLATES

        self.svc = DatabaseService(os.path.join(self.tmp, "refresh.db"))
        self.user = self.svc.create_user("refresh-user", "refreshpw", role="admin")

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
            '/login', data={'username': "refresh-user", 'password': "refreshpw"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 302, "login failed")

    def _seed(self):
        self.svc.save_book(Book(
            abs_id="abs-1", abs_title="Seeded", ebook_filename="abs-1.epub",
            status="active", duration=10000, user_id=self.user.id,
        ))
        self.svc.link_user_book(self.user.id, "abs-1")
        self.svc.save_state(State(
            abs_id="abs-1", client_name="abs", percentage=0.42, timestamp=4200,
            last_updated=1751400000.0, user_id=self.user.id,
        ))
        self.svc.save_state(State(
            abs_id="abs-1", client_name="kosync", percentage=0.30, timestamp=0,
            last_updated=1751399000.0, user_id=self.user.id,
        ))

    def test_returns_the_fields_the_refresh_redraws(self):
        self._seed()
        self._login()

        resp = self.client.get('/api/status/progress')

        self.assertEqual(resp.status_code, 200)
        rows = resp.get_json()["mappings"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["abs_id"], "abs-1")
        self.assertEqual(row["unified_progress"], 42.0)
        self.assertIn("last_sync", row)
        self.assertEqual(row["states"]["abs"]["timestamp"], 4200)
        self.assertEqual(row["states"]["kosync"]["percentage"], 30.0)

    def test_refresh_never_enters_the_full_dashboard_build(self):
        """The drift badge, display metadata, covers and per-service links are
        all rebuilt there — and the refresh reads none of them."""
        self._seed()
        self._login()

        with patch.object(
            ws, '_build_dashboard_mappings',
            side_effect=AssertionError("refresh must not run the full dashboard build"),
        ):
            resp = self.client.get('/api/status/progress')

        self.assertEqual(resp.status_code, 200)

    def test_fields_match_the_full_dashboard_payload(self):
        """Whatever the refresh reads must mean the same thing it did before."""
        self._seed()
        self._login()

        full = self.client.get('/api/status').get_json()["mappings"][0]
        compact = self.client.get('/api/status/progress').get_json()["mappings"][0]

        self.assertEqual(compact["abs_id"], full["abs_id"])
        self.assertEqual(compact["unified_progress"], full["unified_progress"])
        self.assertEqual(compact["last_sync"], full["last_sync"])
        for client_name in ("abs", "kosync"):
            self.assertEqual(
                compact["states"][client_name]["percentage"],
                full["states"][client_name]["percentage"],
            )
            self.assertEqual(
                compact["states"][client_name]["timestamp"],
                full["states"][client_name]["timestamp"],
            )

    def test_ctc_status_updates_card_and_compact_feed_without_reading_map_blobs(self):
        from lxml import html
        from sqlalchemy import event
        from src.services.alignment_service import AlignmentService
        from src.utils.polisher import Polisher

        self._seed()
        self._login()
        self.assertFalse(self.client.get('/api/status/progress').get_json()['mappings'][0]['ctc_aligned'])
        alignment = AlignmentService(self.svc, Polisher())
        alignment._save_alignment('abs-1', [{'char': 0, 'ts': 0.0}, {'char': 100, 'ts': 10.0}], 'ctc')

        statements = []
        def capture_sql(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)
        with self.svc.get_session() as session:
            engine = session.get_bind()
        event.listen(engine, 'before_cursor_execute', capture_sql)
        try:
            compact = self.client.get('/api/status/progress').get_json()['mappings'][0]
        finally:
            event.remove(engine, 'before_cursor_execute', capture_sql)
        self.assertTrue(compact['ctc_aligned'])
        alignment_queries = [sql for sql in statements if 'book_alignments' in sql.lower()]
        self.assertEqual(len(alignment_queries), 1)
        self.assertNotIn('alignment_map_json', alignment_queries[0])
        self.assertTrue(self.client.get('/api/status').get_json()['mappings'][0]['ctc_aligned'])

        page = html.fromstring(self.client.get('/').get_data(as_text=True))
        badge = page.xpath('//span[@class="ctc-alignment-badge"]')[0]
        remap = page.xpath('//button[contains(@class,"remap-alignment-btn")]')[0]
        clear = page.xpath('//button[starts-with(@onclick,"clearPosition(")]')[0]
        self.assertIsNone(badge.get('hidden'))
        self.assertIsNotNone(remap.get('disabled'))
        self.assertEqual(remap.text_content().strip(), '✓ Already using CTC')
        self.assertIsNone(clear.get('disabled'))

        alignment._save_alignment('abs-1', [{'char': 0, 'ts': 0.0}], 'lexical_timed')
        self.assertFalse(self.client.get('/api/status/progress').get_json()['mappings'][0]['ctc_aligned'])
        page = html.fromstring(self.client.get('/').get_data(as_text=True))
        self.assertIsNotNone(page.xpath('//span[@class="ctc-alignment-badge"]')[0].get('hidden'))
        self.assertIsNone(page.xpath('//button[contains(@class,"remap-alignment-btn")]')[0].get('disabled'))

    def test_only_the_users_own_books_are_returned(self):
        self._seed()
        other = self.svc.create_user("other-user", "otherpw", role="user")
        self.svc.save_book(Book(
            abs_id="abs-2", abs_title="Theirs", ebook_filename="abs-2.epub",
            status="active", duration=10000, user_id=other.id,
        ))
        self.svc.link_user_book(other.id, "abs-2")
        self._login()

        rows = self.client.get('/api/status/progress').get_json()["mappings"]

        self.assertEqual([row["abs_id"] for row in rows], ["abs-1"])


if __name__ == "__main__":
    unittest.main()
