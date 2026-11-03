"""Suggestions Search tab: rendered page, queue panel and template guards (Add Book's replacement)."""

import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

from flask import render_template

sys.path.insert(0, str(Path(__file__).parent.parent))

import src.web_server as web_server
from tests.test_webserver import MockContainer

_TEMPLATES = Path(__file__).resolve().parents[1] / "templates"


def _source(name: str = "suggestions.html") -> str:
    return (_TEMPLATES / name).read_text(encoding="utf-8")


def _queue_item(abs_id: str, **extra) -> dict:
    return {
        "abs_id": abs_id, "bridge_key": abs_id, "abs_title": f"Title {abs_id}", "cover_url": "",
        "audio_source": "BookOrbit", "ebook_filename": f"{abs_id}.epub", "ebook_display_name": f"Book {abs_id}",
        **extra,
    }


class SearchPageTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        os.environ["DATA_DIR"] = self.temp_dir
        os.environ["BOOKS_DIR"] = self.temp_dir
        os.environ["TEMPLATE_DIR"] = str(_TEMPLATES)
        self.mock_container = MockContainer()
        self.mock_container.data_dir = lambda: Path(self.temp_dir)
        mock_db = self.mock_container.mock_database_service
        mock_db.get_all_books.return_value = []
        mock_db.get_all_pending_suggestions.return_value = []

        import src.db.migration_utils

        self.original_init_db = src.db.migration_utils.initialize_database
        src.db.migration_utils.initialize_database = lambda _d: self.mock_container.mock_database_service
        self.app, _ = web_server.create_app(test_container=self.mock_container)
        self.app.config["TESTING"] = True
        token = web_server.set_current_user_id(7)
        self.addCleanup(web_server.reset_current_user_id, token)

    def tearDown(self):
        import shutil
        import src.db.migration_utils

        src.db.migration_utils.initialize_database = self.original_init_db
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _render(self, template: str = "suggestions.html", **context) -> str:
        defaults = {
            "suggestions": [], "queue": [], "scan_has_run": False, "scan_in_progress": False,
            "scan_error": None, "scan_stats": {}, "storyteller_enabled": False, "tab_counts": None,
            "is_admin": True, "available_tabs": ["pairs", "ebook_only", "search"],
        }
        defaults.update(context)
        with self.app.test_request_context("/suggestions"):
            return render_template(template, **defaults)


class SearchTabPageTests(SearchPageTestCase):
    def test_admin_page_has_three_tabs_with_search_last(self):
        html = self._render()
        tabs = re.findall(r'<button[^>]*role="tab"[^>]*id="tab-([a-z_]+)"', html)
        self.assertEqual(tabs, ["pairs", "ebook_only", "search"])
        self.assertIn('id="scanForm"', html)
        self.assertIn('id="searchTabForm"', html)

    def test_regular_user_page_has_only_search_and_no_scan_controls(self):
        html = self._render(is_admin=False, available_tabs=["search"])
        tabs = re.findall(r'<button[^>]*role="tab"[^>]*id="tab-([a-z_]+)"', html)
        self.assertEqual(tabs, ["search"])
        for marker in ('id="scanForm"', 'id="scanFullForm"', 'id="filterBar"', 'id="tabCount-pairs"'):
            self.assertNotIn(marker, html)
        self.assertIn('const TABS = ["search"];', html)
        self.assertIn('id="searchTabForm"', html)
        self.assertIn("<h2>Add Book</h2>", html)

    def test_reader_documents_move_to_the_search_tab(self):
        html = self._render(is_admin=False, available_tabs=["search"])
        self.assertIn('id="kosync-documents"', html)
        self.assertIn("/api/me/kosync-documents", html)
        self.assertRegex(html, r'class="reader-docs-wrap" data-only-tab="search"')

    def test_review_form_carries_audio_only_and_readalong(self):
        html = self._render()
        self.assertIn('name="audio_only" id="selected_audio_only"', html)
        self.assertIn('name="readalong_epub_requested" value="true" disabled', html)

    def test_forge_stage_modal_renders_only_with_storyteller(self):
        self.assertNotIn('id="forgeStageModal"', self._render())
        html = self._render(storyteller_enabled=True)
        self.assertIn('id="forgeStageModal"', html)
        self.assertIn('data-stage-mode="cleanup"', html)
        self.assertIn('data-stage-mode="hardlink"', html)

    def test_nav_has_one_suggestions_link_and_no_add_book(self):
        html = self._render(is_admin=False, available_tabs=["search"])
        nav = html[html.index('<nav class="top-nav">'):html.index("</nav>")]
        self.assertEqual(nav.count('href="/suggestions"'), 1)
        self.assertNotIn("/add-book", nav)


class QueuePanelTests(SearchPageTestCase):
    def test_forge_only_offered_for_one_eligible_item(self):
        html = self._render("_match_queue_panel.html", queue=[_queue_item("a")], storyteller_enabled=True)
        self.assertIn('id="forgeOnlyQueueForm"', html)
        self.assertIn('name="action" value="forge_only_queue"', html)

    def test_forge_only_disabled_for_several_items(self):
        html = self._render("_match_queue_panel.html", queue=[_queue_item("a"), _queue_item("b")], storyteller_enabled=True)
        self.assertNotIn('id="forgeOnlyQueueForm"', html)
        self.assertIn("Storyteller edition-only creation is disabled", html)

    def test_storyteller_item_is_not_forge_eligible(self):
        html = self._render("_match_queue_panel.html", queue=[_queue_item("a", storyteller_uuid="st-1")], storyteller_enabled=True)
        self.assertNotIn('id="forgeOnlyQueueForm"', html)

    def test_readalong_item_is_badged(self):
        html = self._render("_match_queue_panel.html", queue=[_queue_item("a", readalong_epub_requested=True)])
        self.assertIn("Read-along", html)


class SearchTemplateGuardTests(unittest.TestCase):
    def test_search_runs_on_submit_not_per_keystroke(self):
        source = _source()
        self.assertIn("$('searchTabForm').addEventListener('submit'", source)
        self.assertNotIn("$('searchTabInput').addEventListener('input'", source)

    def test_find_audiobook_hands_off_to_search_not_add_book(self):
        source = _source()
        self.assertIn("/suggestions?tab=search&ebook_key=", source)
        self.assertNotIn("/add-book", source)

    def test_no_template_links_to_the_removed_page(self):
        for path in _TEMPLATES.glob("*.html"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("url_for('add_book'", text, path.name)
            self.assertNotIn('href="/add-book', text, path.name)


if __name__ == "__main__":
    unittest.main()
