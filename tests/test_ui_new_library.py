"""P3 Library rendering and action parity for the new interface."""

from pathlib import Path
import re
import unittest
from unittest.mock import patch

from flask.signals import template_rendered
import tests.test_ui_new_foundation as foundation
from src.db.models import Book, State


_ROOT = Path(__file__).resolve().parent.parent


class TestNewLibrary(unittest.TestCase):
    tearDown = foundation.TestUiModeRoutes.tearDown
    _login = foundation.TestUiModeRoutes._login

    def setUp(self):
        foundation.TestUiModeRoutes.setUp(self)
        self.app.jinja_loader.loaders.pop(0)
        self.app.jinja_env.cache.clear()
        self.service.save_book(Book(
            abs_id="library-book", abs_title="Library Example", status="active",
            ebook_filename="library-example.epub", duration=3600,
            series_name="Example Series", series_sequence=1,
        ))
        self.service.save_book(Book(
            abs_id="library-finished", abs_title="Finished Example", status="active",
            ebook_filename="finished-example.epub", duration=3600,
            series_name="Example Series", series_sequence=2,
        ))
        self.service.save_state(State(
            abs_id="library-finished", client_name="abs", percentage=1,
            timestamp=3600, last_updated=1793800200, user_id=self.reader.id,
        ))
        self.service.link_user_book(self.reader.id, "library-book")
        self.service.link_user_book(self.reader.id, "library-finished")

    def test_card_heading_does_not_reserve_empty_space(self):
        # A one-line title left a blank band above the progress bar; the heading hugs its content.
        css = (_ROOT / "static/css/next.css").read_text(encoding="utf-8")
        rule = re.search(r"^\.book-heading \{([^}]*)\}", css, re.M).group(1)
        self.assertNotIn("min-height", rule)

    def test_library_batch_actions_execute_sequentially_and_prompt_once(self):
        import shutil
        import subprocess

        node = shutil.which("node")
        if not node:
            self.skipTest("Node is required to execute the library batch actions")
        result = subprocess.run(
            [node, str(Path(__file__).with_suffix(".js"))],
            capture_output=True, text=True, timeout=15,
            cwd=_ROOT,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_select_mode_controls_and_hidden_checkbox_rules_are_new_only(self):
        page = (_ROOT / "templates/next/index.html").read_text(encoding="utf-8")
        classic = (_ROOT / "templates/index.html").read_text(encoding="utf-8")
        styles = (_ROOT / "static/css/next.css").read_text(encoding="utf-8")
        scripts = (_ROOT / "templates/next/_library_scripts.html").read_text(encoding="utf-8")
        toolbar = re.search(r'<section class="library-toolbar".*?</section>', page, re.S).group()
        bar = re.search(r'<div id="selection-bar".*?</div>', page, re.S).group()

        self.assertIn('data-select-mode="off"', page)
        self.assertRegex(toolbar, r'id="select-mode-button"[^>]*aria-pressed="false"')
        self.assertNotIn("selectAllFilteredBooks()", toolbar)
        self.assertIn('id="view-spines"', toolbar)
        self.assertIn('class="book-select-hit"', page)
        self.assertIn('type="checkbox" data-select-book', page)
        self.assertIn('data-select-mode="off"] .book-select-hit', styles)
        self.assertIn('data-view="spines"] #select-mode-button', styles)
        self.assertIn('id="selection-bar"', bar)
        self.assertIn('onclick="selectAllFilteredBooks()"', bar)
        self.assertIn('onclick="clearBookSelection()"', bar)
        for action in ("bulkSyncNow()", "bulkMarkFinished()", "bulkGenerateReadalong()", "bulkDeleteBooks()"):
            with self.subTest(action=action):
                self.assertIn(f'onclick="{action}"', bar)
        self.assertIn('aria-label="{{ _(\'Exit selection\') }}"', bar)
        self.assertIn('onclick="exitBookSelection()"', bar)
        self.assertIn('bar.hidden = !librarySelectMode', scripts)
        self.assertIn('event.stopImmediatePropagation()', scripts)
        self.assertIn("event.key === 'Escape'", scripts)
        self.assertIn("if (view === 'spines') setBookSelectMode(false)", scripts)
        self.assertIn("reloadAfterSelectionAction()", scripts)

        # Select mode is never introduced to the Classic Library template.
        self.assertNotIn('id="select-mode-button"', classic)
        self.assertNotIn('data-select-mode=', classic)
        self.assertNotIn('exitBookSelection()', classic)

    def test_select_mode_click_keyboard_exit_and_bulk_flow(self):
        import shutil
        import subprocess

        node = shutil.which("node")
        if not node:
            self.skipTest("Node is required for selection interaction tests")
        result = subprocess.run(
            [node, str(_ROOT / "tests/test_ui_new_library_select.js")],
            capture_output=True, text=True, timeout=15, cwd=_ROOT,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_reader_gets_new_library_with_classic_default_unchanged(self):
        observed = []

        def capture(_sender, template, **_extra):
            observed.append(template.name)

        template_rendered.connect(capture, self.app)
        try:
            self._login("ui-reader")
            classic = self.client.get("/")
            self.assertEqual(classic.status_code, 200)
            self.assertIn("index.html", observed)
            self.assertNotIn("next/index.html", observed)

            self.service.set_user_credential(self.reader.id, "UI_MODE", "new")
            observed.clear()
            response = self.client.get("/")
            self.assertEqual(response.status_code, 200)
            self.assertIn("next/index.html", observed)
            text = response.get_data(as_text=True)
            self.assertIn("Library Example", text)
            self.assertIn("Finished Example", text)
            self.assertIn("Example Series", text)
            self.assertIn("series:Example Series", text)
            self.assertIn("In progress", text)
            self.assertIn("Not started", text)
            self.assertIn("Finished", text)
            for control in (
                'id="library-page"', 'id="dashboard-search"',
                'id="filter-select"', 'id="series-filter"', 'id="author-filter"',
                'id="sort-select"', 'id="sort-direction"',
                'id="series-grouping-toggle"', 'id="view-covers"',
                'id="view-spines"', 'id="view-list"',
            ):
                with self.subTest(control=control):
                    self.assertIn(control, text)
            self.assertNotIn("Out of sync", text)
            self.assertNotIn("In sync", text)
            unpositioned = re.search(r'<article[^>]*data-abs-id="library-book"[^>]*>.*?</article>', text, re.S).group()
            self.assertIn('class="progress-percent" hidden', unpositioned)
            self.assertIn('class="progress-track" hidden', unpositioned)
            self.assertIn('class="spine-progress" hidden', unpositioned)

            self.service.set_user_credential(self.reader.id, 'SERIES_SHOW_CURRENT_BOOK_DETAIL', 'true')
            lead_view = self.client.get('/').get_data(as_text=True)
            self.assertIn('class="series-strip series-header"', lead_view)
            self.service.delete_book('library-book')
            self.service.delete_book('library-finished')
            empty = self.client.get('/').get_data(as_text=True)
            self.assertIn('No books syncing yet', empty)
            self.assertNotIn('const dashboardSections', empty)
        finally:
            template_rendered.disconnect(capture, self.app)

    def test_ratings_show_both_sources_and_sort_on_their_average_like_classic(self):
        from src import web_server
        original = web_server._build_dashboard_mappings

        def mappings(*args, **kwargs):
            result, progress = original(*args, **kwargs)
            for mapping in result:
                if mapping["abs_id"] == "library-book":
                    mapping.update(storygraph_rating=4.0, goodreads_rating=3.0)
                else:
                    mapping.update(storygraph_rating=None, goodreads_rating=4.4)
            return result, progress

        self.service.set_user_credential(self.reader.id, "UI_MODE", "new")
        self._login("ui-reader")
        with patch.object(web_server, "_build_dashboard_mappings", side_effect=mappings):
            text = self.client.get("/").get_data(as_text=True)
        both = re.search(r'<article[^>]*data-abs-id="library-book"[^>]*>.*?</article>', text, re.S).group()
        gr_only = re.search(r'<article[^>]*data-abs-id="library-finished"[^>]*>.*?</article>', text, re.S).group()
        self.assertIn('data-rating="3.5"', both)
        self.assertIn("SG 4.0 · GR 3.0", both)
        # Ratings sit under the title; CTC and read-along badges live in the book drawer, not on the card.
        self.assertRegex(both, r'class="book-heading"><h3>.*?</h3><span class="book-ratings"[^>]*>SG 4.0 · GR 3.0')
        self.assertNotIn("cover-badge", both)
        self.assertNotIn("ctc-alignment-badge", both)
        self.assertIn('data-rating="4.4"', gr_only)
        self.assertIn("GR 4.4", gr_only)
        self.assertNotIn("SG ", gr_only)

    def test_out_of_sync_line_uses_only_that_books_app_positions(self):
        from src import web_server
        original = web_server._build_dashboard_mappings

        def mappings(*args, **kwargs):
            result, progress = original(*args, **kwargs)
            for mapping in result:
                if mapping["abs_id"] == "library-book":
                    mapping.update(
                        is_out_of_sync=True, sync_warning_pct=4.9,
                        abs_pct=27.0, kosync_pct=22.1,
                        states={"abs": {"percentage": 27.0}, "kosync": {"percentage": 22.1}},
                    )
                else:
                    mapping["is_out_of_sync"] = False
            return result, progress

        self.service.set_user_credential(self.reader.id, "UI_MODE", "new")
        self._login("ui-reader")
        with patch.object(web_server, "_build_dashboard_mappings", side_effect=mappings):
            response = self.client.get("/")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True)[:2000])
        text = response.get_data(as_text=True)

        bad_card = re.search(r'<article[^>]*data-abs-id="library-book"[^>]*>.*?</article>', text, re.S).group()
        good_card = re.search(r'<article[^>]*data-abs-id="library-finished"[^>]*>.*?</article>', text, re.S).group()
        self.assertIn("Out of sync", bad_card)
        self.assertIn("KOReader", bad_card)
        self.assertIn("Audiobookshelf", bad_card)
        self.assertIn("22.1%", bad_card)
        self.assertIn("27.0%", bad_card)
        self.assertNotIn("Out of sync", good_card)
        self.assertNotIn("In sync", good_card)

    def test_library_templates_keep_filters_actions_polling_and_shared_drawer_scripts(self):
        page = (_ROOT / "templates/next/index.html").read_text(encoding="utf-8")
        scripts = (_ROOT / "templates/next/_library_scripts.html").read_text(encoding="utf-8")
        classic = (_ROOT / "templates/index.html").read_text(encoding="utf-8")

        for value in ("all", "paired", "audiobook_only", "ebook_only", "needs_attention"):
            self.assertIn(f'data-format="{value}"', page)
        for value in ("all", "paired", "has_audio", "audiobook_only", "ebook_only", "needs_attention", "title", "author", "series", "progress", "status", "last_sync", "added", "rating"):
            self.assertIn(f'<option value="{value}"', page)
        for label in (
            "Title", "Author", "Series", "Progress", "Status",
            "Last Synced", "Date Added", "Rating", "Covers", "Spines", "List",
        ):
            self.assertIn(label, page)
        self.assertIn('id="sort-direction"', page)
        self.assertIn('id="series-grouping-toggle"', page)
        self.assertIn("openBookDrawer", page)
        self.assertIn("Select all filtered", page)
        for hook in (
            "selectAllFilteredBooks", "clearBookSelection", "bulkSyncNow",
            "bulkMarkFinished", "bulkGenerateReadalong", "bulkDeleteBooks",
            "refreshDashboard",
        ):
            with self.subTest(hook=hook):
                self.assertIn(hook, scripts)
        self.assertIn("{% include 'next/_book_scripts.html' %}", page)
        for endpoint in (
            "/api/status/progress", "/api/forge/active", "/api/sync-now/",
            "/api/mark-complete/", "/api/readalong-epub/", "/delete/",
        ):
            with self.subTest(endpoint=endpoint):
                self.assertIn(endpoint, scripts)
                self.assertIn(endpoint, classic)
        for key in (
            "abs_kosync_sort", "abs_kosync_sort_state", "abs_kosync_filters",
            "abs_kosync_series_grouping", "not_started_expanded", "series_expanded_",
        ):
            with self.subTest(storage_key=key):
                self.assertIn(key, scripts)
                self.assertIn(key, classic)
        self.assertRegex(
            scripts,
            r"(?s)async function runSelectedBooks.*?for \(let i = 0; i < ids.length; i\+\+\).*?await action\(ids\[i\]\)",
        )
        self.assertRegex(scripts, r"(?s)bulkMarkFinished.*?confirm\(")
        self.assertRegex(scripts, r"(?s)bulkDeleteBooks.*?confirm\(")
