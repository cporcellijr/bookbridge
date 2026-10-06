"""Tests for the "what's new after upgrade" banner/page.

Covers the pure decision function and notes parser in
``src/services/whats_new.py`` directly, plus the Flask routes and the
Library-page banner context wired up in ``src/web_server.py``.
"""

import os
import shutil
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from src.services import whats_new
from src.utils.time_utils import utcnow

_TEMPLATES = str(Path(__file__).parent.parent / "templates")


class TestShouldShowBannerDecision(unittest.TestCase):
    """Pure function: no Flask, no DB, no filesystem."""

    def setUp(self):
        self.now = utcnow()
        self.earlier = self.now - timedelta(days=1)
        self.later = self.now + timedelta(days=1)

    def test_dev_version_is_skipped(self):
        self.assertEqual(
            whats_new.should_show_banner("dev", None, self.earlier, self.now), "hide"
        )

    def test_dev_with_build_number_is_skipped(self):
        self.assertEqual(
            whats_new.should_show_banner("dev 1267", "7.7.0", self.earlier, self.now),
            "hide",
        )

    def test_stored_matches_running_version_hidden(self):
        self.assertEqual(
            whats_new.should_show_banner("7.8.0", "7.8.0", self.earlier, self.now),
            "hide",
        )

    def test_stored_older_version_shown(self):
        self.assertEqual(
            whats_new.should_show_banner("7.8.0", "7.7.0", self.earlier, self.now),
            "show",
        )

    def test_no_stored_user_created_before_process_start_shown(self):
        # Existing user, never had a seen-version recorded, upgraded.
        self.assertEqual(
            whats_new.should_show_banner("7.8.0", None, self.earlier, self.now),
            "show",
        )

    def test_no_stored_user_created_after_process_start_stores_baseline(self):
        # Brand-new account created by this same process.
        self.assertEqual(
            whats_new.should_show_banner("7.8.0", None, self.later, self.now),
            "store_current",
        )

    def test_no_stored_no_user_created_at_stores_baseline(self):
        self.assertEqual(
            whats_new.should_show_banner("7.8.0", None, None, self.now),
            "store_current",
        )

    def test_beta_version_treated_as_a_release_not_skipped(self):
        self.assertEqual(
            whats_new.should_show_banner("7.9.0-beta.1", None, self.earlier, self.now),
            "show",
        )


class TestReleaseNotesParsing(unittest.TestCase):
    """Notes loading/parsing: Action Required extraction + HTML rendering."""

    SAMPLE_NOTES = """# Release Notes - 7.8.0

Intro paragraph describing the release.

## Action Required

- Re-download **BridgeSync 0.9.6** on every KOReader device that uses the plugin,
  then restart KOReader.

## What's New

- **Feature one.** Does a thing.
- Feature two does another thing.
"""

    def setUp(self):
        # Cache is module-global; save/restore so other tests never see our
        # synthetic notes (or vice versa).
        self._orig_cache = whats_new._notes_cache
        whats_new._notes_cache = None
        self.tmp_dir = tempfile.mkdtemp()

    def tearDown(self):
        whats_new._notes_cache = self._orig_cache
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _write_notes(self, text: str) -> Path:
        path = Path(self.tmp_dir) / "RELEASE_NOTES.md"
        path.write_text(text, encoding="utf-8")
        return path

    def test_action_required_bullets_extracted_without_bold_markers(self):
        path = self._write_notes(self.SAMPLE_NOTES)
        with patch.object(whats_new, "_find_notes_path", return_value=path):
            items = whats_new.get_action_required_items()
        self.assertEqual(
            items,
            [
                "Re-download BridgeSync 0.9.6 on every KOReader device that uses "
                "the plugin, then restart KOReader."
            ],
        )
        self.assertNotIn("**", items[0])

    def test_rendered_html_puts_action_required_before_whats_new(self):
        path = self._write_notes(self.SAMPLE_NOTES)
        with patch.object(whats_new, "_find_notes_path", return_value=path):
            html = whats_new.get_release_notes_html()
        self.assertIn("Action Required", html)
        self.assertIn("Feature one", html)
        self.assertIn("Feature two", html)
        action_idx = html.index("Action Required")
        feature_idx = html.index("Feature one")
        self.assertLess(
            action_idx, feature_idx,
            "Action Required section must render before What's New content",
        )

    def test_missing_file_returns_none_and_does_not_crash(self):
        with patch.object(whats_new, "_find_notes_path", return_value=None):
            self.assertIsNone(whats_new.load_release_notes())
            self.assertEqual(whats_new.get_action_required_items(), [])
            self.assertEqual(whats_new.get_release_notes_html(), "")

    def test_notes_without_action_required_section_has_no_bullets(self):
        text = "# Release Notes - 1.0.0\n\nIntro.\n\n## What's New\n\n- Something new.\n"
        path = self._write_notes(text)
        with patch.object(whats_new, "_find_notes_path", return_value=path):
            self.assertEqual(whats_new.get_action_required_items(), [])
            html = whats_new.get_release_notes_html()
        self.assertIn("Something new", html)


class TestWhatsNewRoutes(unittest.TestCase):
    """Flask routes + Library-page banner, via a real DatabaseService and a
    logged-in session (mirrors tests/test_dashboard_book_scoping.py)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ['DATA_DIR'] = self.tmp
        os.environ['BOOKS_DIR'] = self.tmp
        self._orig_template_dir = os.environ.get('TEMPLATE_DIR')
        os.environ['TEMPLATE_DIR'] = _TEMPLATES

        from src.db.database_service import DatabaseService
        self.svc = DatabaseService(os.path.join(self.tmp, "whats-new.db"))
        self.user = self.svc.create_user("wn-user", "wnpassword", role="user")

        from tests.test_webserver import MockContainer
        self.mock_container = MockContainer()
        self.mock_container.mock_database_service = self.svc

        import src.db.migration_utils
        self._orig_init = src.db.migration_utils.initialize_database
        src.db.migration_utils.initialize_database = lambda data_dir: self.svc

        # Module-global notes cache: save/restore so other test files' runs
        # (order-independent suite) never see a stale/patched value here.
        self._orig_notes_cache = whats_new._notes_cache
        whats_new._notes_cache = None
        self._orig_process_started_at = whats_new.PROCESS_STARTED_AT

        from src.web_server import create_app
        self.app, _ = create_app(test_container=self.mock_container)
        self.app.config['TESTING'] = True
        self.app.config['LOGIN_DISABLED'] = False
        self.client = self.app.test_client()

    def tearDown(self):
        import src.db.migration_utils
        src.db.migration_utils.initialize_database = self._orig_init
        whats_new._notes_cache = self._orig_notes_cache
        whats_new.PROCESS_STARTED_AT = self._orig_process_started_at
        if self._orig_template_dir is None:
            os.environ.pop('TEMPLATE_DIR', None)
        else:
            os.environ['TEMPLATE_DIR'] = self._orig_template_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _login(self):
        resp = self.client.post(
            '/login', data={'username': 'wn-user', 'password': 'wnpassword'},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 302, "login failed")

    # ---- dismiss endpoint ---------------------------------------------

    def test_dismiss_stores_seen_version_for_current_user(self):
        self._login()
        with patch('src.web_server.APP_VERSION', '7.8.0'):
            resp = self.client.post('/whats-new/dismiss')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json().get('ok'))
        creds = self.svc.get_user_credentials(self.user.id)
        self.assertEqual(creds.get('WHATS_NEW_SEEN_VERSION'), '7.8.0')

    def test_dismiss_requires_login(self):
        resp = self.client.post('/whats-new/dismiss', follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login', resp.headers.get('Location', ''))

    # ---- /whats-new page -------------------------------------------------

    def test_whats_new_page_renders_and_marks_seen(self):
        self._login()
        with patch('src.web_server.APP_VERSION', '7.8.0'):
            resp = self.client.get('/whats-new')
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("What", html)
        self.assertIn("All releases", html)
        self.assertIn("https://github.com/cporcellijr/bookbridge/releases", html)
        creds = self.svc.get_user_credentials(self.user.id)
        self.assertEqual(creds.get('WHATS_NEW_SEEN_VERSION'), '7.8.0')

    # ---- Library-page banner ----------------------------------------------

    def test_index_shows_banner_when_stored_version_is_older(self):
        self.svc.set_user_credential(self.user.id, 'WHATS_NEW_SEEN_VERSION', '7.7.0')
        self._login()
        with patch('src.web_server.APP_VERSION', '7.8.0'):
            resp = self.client.get('/')
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("BookBridge updated to 7.8.0", html)

    def test_index_hides_banner_when_stored_version_matches(self):
        self.svc.set_user_credential(self.user.id, 'WHATS_NEW_SEEN_VERSION', '7.8.0')
        self._login()
        with patch('src.web_server.APP_VERSION', '7.8.0'):
            resp = self.client.get('/')
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertNotIn("BookBridge updated to 7.8.0", html)

    def test_index_new_user_no_stored_value_silently_stores_baseline(self):
        # self.user was created during setUp, i.e. after whats_new module
        # import (PROCESS_STARTED_AT) -- the "brand-new account" branch.
        self._login()
        with patch('src.web_server.APP_VERSION', '7.8.0'):
            resp = self.client.get('/')
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertNotIn("BookBridge updated to 7.8.0", html)
        creds = self.svc.get_user_credentials(self.user.id)
        self.assertEqual(creds.get('WHATS_NEW_SEEN_VERSION'), '7.8.0')

    def test_index_existing_user_no_stored_value_shows_banner(self):
        # Simulate an existing user (created before this process started) by
        # moving the recorded process-start time to after their creation.
        whats_new.PROCESS_STARTED_AT = self.user.created_at + timedelta(hours=1)
        self._login()
        with patch('src.web_server.APP_VERSION', '7.8.0'):
            resp = self.client.get('/')
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("BookBridge updated to 7.8.0", html)
        # Banner shown but not yet marked seen -- only view/dismiss do that.
        creds = self.svc.get_user_credentials(self.user.id)
        self.assertIsNone(creds.get('WHATS_NEW_SEEN_VERSION'))

    def test_index_action_required_bullet_shown_inline(self):
        # Synthetic notes: the repo's RELEASE_NOTES.md only has an Action
        # Required section in releases that need one.
        notes_path = Path(self.tmp) / "RELEASE_NOTES.md"
        notes_path.write_text(TestReleaseNotesParsing.SAMPLE_NOTES, encoding="utf-8")
        self.svc.set_user_credential(self.user.id, 'WHATS_NEW_SEEN_VERSION', '7.7.0')
        self._login()
        with patch('src.web_server.APP_VERSION', '7.8.0'), \
                patch.object(whats_new, "_find_notes_path", return_value=notes_path):
            resp = self.client.get('/')
        html = resp.get_data(as_text=True)
        self.assertIn("Re-download BridgeSync", html)

    def test_index_no_banner_for_dev_build(self):
        self._login()
        with patch('src.web_server.APP_VERSION', 'dev 1267'):
            resp = self.client.get('/')
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertNotIn("BookBridge updated to", html)
        self.assertNotIn('id="whats-new-banner"', html)
        creds = self.svc.get_user_credentials(self.user.id)
        self.assertIsNone(creds.get('WHATS_NEW_SEEN_VERSION'))


if __name__ == '__main__':
    unittest.main()
