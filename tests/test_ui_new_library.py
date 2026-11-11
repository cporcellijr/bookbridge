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

    def test_spine_colours_come_from_the_cover(self):
        import shutil
        import subprocess

        node = shutil.which("node")
        if not node:
            self.skipTest("Node is required to execute the spine colour picker")
        result = subprocess.run(
            [node, str(_ROOT / "tests/test_ui_new_library_spines.js")],
            capture_output=True, text=True, timeout=15, cwd=_ROOT,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_spine_width_follows_book_length_and_reading_books_face_out(self):
        from src import web_server
        original = web_server._build_dashboard_mappings
        cases = {
            "library-book": dict(storygraph_pages=400, states={"abs": {"percentage": 30.0}}, unified_progress=30.0),
            "hardcover-only": dict(hardcover_pages=2000, duration=0),
            "audio-only": dict(duration=36000),
            "no-length": dict(duration=0),
        }

        def mappings(*args, **kwargs):
            result, progress = original(*args, **kwargs)
            base = next(m for m in result if m["abs_id"] == "library-book")
            for abs_id in ("hardcover-only", "audio-only", "no-length"):
                result.append({**base, "abs_id": abs_id, "display_title": abs_id, "states": {}, "unified_progress": 0.0})
            for mapping in result:
                mapping.update(cases.get(mapping["abs_id"], {}))
            return result, progress

        self.service.set_user_credential(self.reader.id, "UI_MODE", "new")
        self._login("ui-reader")
        with patch.object(web_server, "_build_dashboard_mappings", side_effect=mappings):
            text = self.client.get("/").get_data(as_text=True)
        tag = lambda abs_id: re.search(rf'<article[^>]*data-abs-id="{abs_id}"[^>]*>', text).group()

        self.assertIn('style="--spine-w:46px"', tag("library-book"))
        self.assertIn('style="--spine-w:60px"', tag("hardcover-only"))
        self.assertIn('style="--spine-w:41px"', tag("audio-only"))
        self.assertNotIn("--spine-w", tag("no-length"))
        self.assertIn(" data-reading ", tag("library-book"))
        for abs_id in ("library-finished", "audio-only"):
            with self.subTest(abs_id=abs_id):
                self.assertNotIn("data-reading", tag(abs_id))
        css = (_ROOT / "static/css/next.css").read_text(encoding="utf-8")
        self.assertIn('.library-page[data-view="spines"] .book-card[data-reading] .cover-wrap { display: grid;', css)

        # The real dashboard row carries StoryGraph's page count through to the spine.
        from src.db.models import StorygraphDetails
        self.service.save_storygraph_details(StorygraphDetails(abs_id="library-book", storygraph_book_id="sg-1", storygraph_pages=400))
        unpatched = self.client.get("/").get_data(as_text=True)
        self.assertIn('style="--spine-w:46px"', re.search(r'<article[^>]*data-abs-id="library-book"[^>]*>', unpatched).group())

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
        self.assertIn("event.key !== 'Escape'", scripts)
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

    def test_library_filter_moves_into_the_top_bar_on_wide_screens(self):
        import shutil
        import subprocess

        node = shutil.which("node")
        if not node:
            self.skipTest("Node is required for the search placement test")
        result = subprocess.run(
            [node, str(_ROOT / "tests/test_ui_new_library_search.js")],
            capture_output=True, text=True, timeout=15, cwd=_ROOT,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_header_has_no_discover_button_but_the_empty_library_keeps_one(self):
        from pathlib import Path
        page = (Path(__file__).resolve().parent.parent / "templates/next/index.html").read_text(encoding="utf-8")
        header = page[page.index('<header class="page-head">'):page.index("</header>")]
        self.assertNotIn("Discover books", header)
        empty = page[page.index('<section class="empty-state">'):]
        self.assertIn("Discover books", empty)

    def test_library_has_one_search_box_with_the_ctrl_k_launcher_inside(self):
        self._login("ui-reader")
        self.service.set_user_credential(self.reader.id, "UI_MODE", "new")
        text = self.client.get("/").get_data(as_text=True)
        slot = text.index('data-library-search-slot')
        self.assertLess(slot, text.index('id="bb-search-open"'))
        self.assertLess(text.index('id="bb-search-open"'), text.index('</div>', slot))
        self.assertEqual(text.count('id="dashboard-search"'), 1)
        self.assertEqual(text.count('id="bb-search-open"'), 1)
        stats = self.client.get("/stats").get_data(as_text=True)
        self.assertNotIn('data-library-search-slot', stats)
        self.assertIn('id="bb-search-open"', stats)

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

    def test_cover_carries_the_format_strip_in_place_of_the_last_app_line(self):
        self.service.record_sync_marker("library-finished", "ABS", 1793800200.0, user_id=self.reader.id)
        self.service.set_user_credential(self.reader.id, "UI_MODE", "new")
        self._login("ui-reader")
        text = self.client.get("/").get_data(as_text=True)

        card = re.search(r'<article[^>]*data-abs-id="library-finished"[^>]*>.*?</article>', text, re.S).group()
        strip = re.search(r'<span class="format-strip">.*?</a>', card, re.S).group()
        self.assertEqual(re.findall(r'data-slot="(\w+)"', strip), ["ebook", "audio", "readalong", "koreader"])
        self.assertRegex(strip, r'class="format-slot is-last" data-slot="audio"')
        self.assertRegex(strip, r'class="format-slot is-faint" data-slot="koreader"')
        self.assertIn("Audiobook · Audiobookshelf 100%", strip)
        self.assertIn("KOReader · not linked", strip)
        self.assertNotIn("last-app", text)
        self.assertNotIn("Last: ", card)

    def test_touch_input_shows_the_format_strip_and_a_tapped_icon_its_tip(self):
        # iPadOS matches (hover: hover), so the hover-only strip stayed invisible and a tap opened the book.
        css = (_ROOT / "static/css/next.css").read_text(encoding="utf-8")
        hover_block = re.search(r"@media \(hover: hover\) \{(.*?)\n\}", css, re.S).group(1)
        self.assertIn('html[data-input="touch"] .format-strip', hover_block)
        self.assertIn(".format-slot.tip-open .format-tip", css)
        script = (_ROOT / "templates/next/_library_scripts.html").read_text(encoding="utf-8")
        self.assertIn("event.pointerType === 'mouse' ? 'mouse' : 'touch'", script)
        self.assertIn("slot.classList.toggle('tip-open')", script)

    def test_cover_shows_progress_badge_and_line_only_for_a_position_above_zero(self):
        from src import web_server
        original = web_server._build_dashboard_mappings
        cases = {
            "library-book": dict(states={"abs": {"percentage": 47.6}, "kosync": {"percentage": 0.0}}, unified_progress=47.6),
            "cleared": dict(states={"abs": {"percentage": 0.0}, "kosync": {"percentage": 0}}, unified_progress=0.0),
            "unstarted": dict(states={}, unified_progress=0.0),
        }

        def mappings(*args, **kwargs):
            result, progress = original(*args, **kwargs)
            base = next(m for m in result if m["abs_id"] == "library-book")
            for abs_id in ("cleared", "unstarted"):
                result.append({**base, "abs_id": abs_id, "display_title": abs_id})
            for mapping in result:
                mapping.update(cases.get(mapping["abs_id"], {}))
            return result, progress

        self.service.set_user_credential(self.reader.id, "UI_MODE", "new")
        self._login("ui-reader")
        with patch.object(web_server, "_build_dashboard_mappings", side_effect=mappings):
            text = self.client.get("/").get_data(as_text=True)
        card = lambda abs_id: re.search(rf'<article[^>]*data-abs-id="{abs_id}"[^>]*>.*?</article>', text, re.S).group()
        shown = lambda html: sorted(name for name, attrs in re.findall(r'class="(progress-percent|progress-done|progress-track|spine-progress)"([^>]*)>', html) if "hidden" not in attrs)

        # Clear progress leaves 0% reset snapshots; neither that nor no state at all shows progress.
        for abs_id in ("cleared", "unstarted"):
            with self.subTest(abs_id=abs_id):
                self.assertEqual(shown(card(abs_id)), [])

        reading = card("library-book")
        self.assertEqual(shown(reading), ["progress-percent", "progress-track", "spine-progress"])
        self.assertIn('class="progress-percent" >47%</span>', reading)
        self.assertIn('style="width:47.6%"', reading)
        # Progress sits on the cover; under it come the title then the author.
        self.assertRegex(reading, r'(?s)class="cover-wrap">.*class="progress-percent".*</div>\s*<div class="book-copy"><div class="book-heading"><h3>.*?</h3><span class="book-author">')
        self.assertNotIn("progress-line", reading)

        finished = card("library-finished")
        self.assertEqual(shown(finished), ["progress-done", "spine-progress"])
        self.assertIn('class="progress-done" role="img" aria-label="Finished"', finished)
        self.assertNotIn('class="state-label"', finished)

    def test_live_refresh_hides_the_cover_progress_again_at_zero(self):
        import shutil
        import subprocess

        node = shutil.which("node")
        if not node:
            self.skipTest("Node is required to execute the library refresh")
        result = subprocess.run(
            [node, str(_ROOT / "tests/test_ui_new_library_refresh.js")],
            capture_output=True, text=True, timeout=15, cwd=_ROOT,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

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
        self.assertRegex(both, r'class="book-heading"><h3>.*?</h3><span class="book-author">[^<]*</span><span class="book-ratings"[^>]*>SG 4.0 · GR 3.0')
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
