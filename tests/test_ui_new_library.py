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

    def test_reader_style_renders_before_paint_without_toolbar_switch(self):
        self.service.set_user_credential(self.reader.id, "UI_MODE", "new")
        self._login("ui-reader")
        for preference, view in (("cards", "covers"), ("spines", "spines"),
                                 ("list", "list"), ("faceout", "spines")):
            with self.subTest(preference=preference):
                self.service.set_user_credential(self.reader.id, "UI_LIBRARY_STYLE", preference)
                response = self.client.get("/")
                self.assertEqual(response.status_code, 200)
                markup = response.get_data(as_text=True)
                self.assertRegex(markup, rf'id="library-page"[^>]*data-view="{view}"')
                self.assertIn(f'data-library-style="{preference}"', markup)
                self.assertNotIn('class="view-switch"', markup)
        self.assertIsNone(self.service.get_user_credential(self.admin.id, "UI_LIBRARY_STYLE"))

    def test_faceout_uses_real_covers_on_wood_planks_with_three_phone_columns(self):
        css = (_ROOT / "static/css/next.css").read_text(encoding="utf-8")
        scoped = '.library-page[data-library-style="faceout"][data-view="spines"]'
        self.assertIn(scoped + " .book-grid {", css)
        self.assertIn(scoped + " .book-card .cover-image {", css)
        self.assertIn("object-fit: cover; filter: none;", css)
        self.assertIn(scoped + " .book-card .book-copy h3 {", css)
        self.assertIn("writing-mode: horizontal-tb; transform: none;", css)
        self.assertIn(scoped + " .book-card .book-author {", css)
        self.assertIn("grid-template-columns: repeat(3, minmax(0, 1fr));", css)
        self.assertIn("--shelf-pitch: 250px;", css)
        self.assertIn(':not([data-library-style="faceout"]) .book-card:not([data-reading]) .cover-wrap > :not(.cover-image)', css)

    def test_faceout_load_initializer_reuses_spine_behavior(self):
        import shutil
        import subprocess

        node = shutil.which("node")
        if not node:
            self.skipTest("Node is required to execute the library style initialization")
        result = subprocess.run(
            [node, str(_ROOT / "tests/test_ui_new_library_style.js")],
            capture_output=True, text=True, timeout=15, cwd=_ROOT,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_rendered_new_library_uses_thumbnails_with_original_proxy_fallback(self):
        from src import web_server

        original = web_server._build_dashboard_mappings

        def covers(*args, **kwargs):
            mappings, progress = original(*args, **kwargs)
            for mapping in mappings:
                mapping["kosync_doc_id"] = "cover" + mapping["abs_id"].replace("-", "")
                mapping["cover_url"] = "/api/cover-proxy/" + mapping["abs_id"]
            return mappings, progress

        self._login("ui-reader")
        with patch.object(web_server, "_build_dashboard_mappings", side_effect=covers):
            self.service.set_user_credential(self.reader.id, "UI_MODE", "new")
            response = self.client.get("/")
            self.assertEqual(response.status_code, 200)
            html = response.get_data(as_text=True)
            self.assertIn('data-thumb-url="/covers/thumb/coverlibrarybook.jpg"', html)
            self.assertIn('data-thumb-url="/covers/thumb/coverlibraryfinished.jpg"', html)
            self.assertIn('hidden loading="lazy" decoding="async"', html)
            self.assertIn('/static/js/next/cover-loader.js', html)
            self.assertIn('this.src=&#34;/api/cover-proxy/library-book&#34;', html)
            # One thumbnail is used in the collapsed series preview too.
            self.assertGreaterEqual(html.count('data-thumb-url="/covers/thumb/coverlibrarybook.jpg"'), 2)

            self.service.set_user_credential(self.reader.id, "UI_MODE", "classic")
            classic = self.client.get("/").get_data(as_text=True)
            self.assertIn('src="/covers/coverlibrarybook.jpg"', classic)
            self.assertNotIn('data-thumb-url="/covers/thumb/coverlibrarybook.jpg"', classic)

    def test_ready_thumbnails_render_directly_with_versioned_urls(self):
        from src import web_server

        original = web_server._build_dashboard_mappings

        def covers(*args, **kwargs):
            mappings, progress = original(*args, **kwargs)
            for mapping in mappings:
                mapping["kosync_doc_id"] = "cover" + mapping["abs_id"].replace("-", "")
            return mappings, progress

        self._login("ui-reader")
        web_server._thumb_versions["coverlibrarybook"] = ("a-b", float("inf"))
        try:
            with patch.object(web_server, "_build_dashboard_mappings", side_effect=covers):
                self.service.set_user_credential(self.reader.id, "UI_MODE", "new")
                html = self.client.get("/").get_data(as_text=True)
        finally:
            web_server._thumb_versions.clear()
        # Card and series preview both skip the hidden probe for the ready cover.
        self.assertGreaterEqual(html.count('src="/covers/thumb/coverlibrarybook.jpg?v=a-b" alt=""'), 2)
        self.assertNotIn('data-thumb-url="/covers/thumb/coverlibrarybook.jpg"', html)
        self.assertIn('data-thumb-url="/covers/thumb/coverlibraryfinished.jpg" alt="" hidden', html)

    def test_book_drawer_eases_in_instead_of_flashing_dark(self):
        # The drawer opens before its frame loads; an instant dim + empty panel flashed black.
        css = (_ROOT / "static/css/next.css").read_text(encoding="utf-8")
        self.assertRegex(css, r"\.book-drawer-host\[open\] \{ animation: drawer-in ")
        self.assertRegex(css, r"\.book-drawer-host\[open\]::backdrop \{ animation: drawer-dim-in ")
        self.assertIn("@keyframes drawer-dim-in { from { background: transparent; } }", css)

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
            "no-length": dict(duration=0, display_title="A Very Long Title That Goes On And On Forever"),
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

        self.assertIn('style="--spine-w:46px;', tag("library-book"))
        self.assertIn('style="--spine-w:64px;', tag("hardcover-only"))
        # A short book (an hour of audio, about 30 pages) still gets a spine wide enough for its title.
        self.assertIn('style="--spine-w:36px;', tag("library-finished"))
        self.assertIn('style="--spine-w:41px;', tag("audio-only"))
        self.assertNotIn("--spine-w", tag("no-length"))
        # Title size follows the spine: a short title fills it, a long one shrinks to fit more of itself.
        self.assertIn("--spine-font:26px", tag("audio-only"))
        self.assertIn("--spine-font:20px", tag("library-book"))
        self.assertIn('style="--spine-font:14px;--spine-h:', tag("no-length"))
        self.assertIn(" data-reading ", tag("library-book"))
        for abs_id in ("library-finished", "audio-only"):
            with self.subTest(abs_id=abs_id):
                self.assertNotIn("data-reading", tag(abs_id))
        css = (_ROOT / "static/css/next.css").read_text(encoding="utf-8")
        self.assertIn('.library-page[data-view="spines"] .book-card[data-reading] .cover-wrap { display: grid;', css)
        # A spine carries its cover's artwork behind the title; the sampled colour stays as the fallback.
        self.assertIn('.library-page[data-view="spines"] .book-card:not([data-reading]) .cover-wrap { display: block; position: absolute; inset: 0;', css)
        self.assertIn('.book-card:has(.cover-image:not([hidden])) .book-open { color: #f6f1e7;', css)
        self.assertIn('.book-card:not([data-reading]) .cover-image { right: auto; width: calc(var(--spine-w, 42px) * 8); object-fit: fill; filter: brightness(.8) saturate(1.2); }', css)
        # The physical spine uses the same dark outline in both themes, over its cover edge.
        self.assertIn('box-shadow: 0 0 0 1px rgba(0,0,0,.5), 7px -1px 12px -4px var(--shelf-shadow);', css)
        self.assertNotIn('box-shadow: 0 0 0 1px rgba(255,255,255,.16)', css)

        # The real dashboard row carries StoryGraph's page count through to the spine.
        from src.db.models import StorygraphDetails
        self.service.save_storygraph_details(StorygraphDetails(abs_id="library-book", storygraph_book_id="sg-1", storygraph_pages=400))
        unpatched = self.client.get("/").get_data(as_text=True)
        self.assertIn('style="--spine-w:46px;', re.search(r'<article[^>]*data-abs-id="library-book"[^>]*>', unpatched).group())

    def test_spine_height_tooltip_band_and_face_out_cover_rules(self):
        import zlib
        from src import web_server
        original = web_server._build_dashboard_mappings
        order = {"shift": False}

        def mappings(*args, **kwargs):
            result, progress = original(*args, **kwargs)
            base = next(m for m in result if m["abs_id"] == "library-book")
            base["display_subtitle"] = "A Subtitle"
            if order["shift"]:
                # Three more unstarted books ahead of it move library-book further along the shelf.
                result[:0] = [{**base, "abs_id": f"ahead-{n}", "display_title": f"Ahead {n}", "series_name": None} for n in range(3)]
            return result, progress

        self.service.set_user_credential(self.reader.id, "UI_MODE", "new")
        self._login("ui-reader")
        heights, positions = [], []
        with patch.object(web_server, "_build_dashboard_mappings", side_effect=mappings):
            for shift in (False, True):
                order["shift"] = shift
                text = self.client.get("/").get_data(as_text=True)
                ids = re.findall(r'<article[^>]*data-abs-id="([^"]+)"', text)
                positions.append(ids.index("library-book"))
                heights.append({abs_id: re.search(rf'<article[^>]*--spine-h:(\d+)px[^>]*data-abs-id="{abs_id}"', text).group(1) for abs_id in ("library-book", "library-finished")})
        # Each book keeps its own height whatever its position on the shelf, picked from the four shelf heights.
        self.assertNotEqual(positions[0], positions[1])
        self.assertEqual(heights[0], heights[1])
        for abs_id, height in heights[0].items():
            with self.subTest(abs_id=abs_id):
                self.assertEqual(int(height), (196, 204, 214, 224)[zlib.crc32(abs_id.encode("utf-8")) % 4])
        # Hovering a spine shows its full title, subtitle included.
        self.assertIn('title="Library Example: A Subtitle"', re.search(r'<a class="book-open"[^>]*>', text.split('data-abs-id="library-book"', 1)[1]).group())

        css = (_ROOT / "static/css/next.css").read_text(encoding="utf-8")
        spines = '.library-page[data-view="spines"] '
        self.assertIn(spines + ".book-card { --spine: #41546b; flex: none; display: flex; flex-direction: column; width: var(--spine-w, 42px); height: var(--spine-h, 214px);", css)
        self.assertNotRegex(css, r'data-view="spines"\] \.book-card:nth-child\([^)]*\) \{ height')
        # Cover art replaces the flat top band; a coverless spine keeps it.
        self.assertIn(spines + ".book-card:not([data-reading]):has(.cover-image:not([hidden]))::before { display: none; }", css)
        # The finished tick is gold and shadowed like the title when it sits on cover art.
        self.assertIn(spines + ".spine-progress { display: block; padding: 0 0 8px; text-align: center; line-height: 0; color: #F7C766; }", css)
        self.assertIn(spines + ".book-card:has(.cover-image:not([hidden])) .spine-progress { filter: drop-shadow(", css)
        # A square cover fills the face-out card instead of leaving bands.
        self.assertIn(spines + ".book-card[data-reading] .cover-image { object-fit: cover; }", css)
        self.assertIn(spines + ".book-card[data-reading] { width: 142px; height: 214px;", css)
        # Phones: two face-out covers fit one shelf (base 214px height + 10px top = 168 + 56).
        self.assertIn("@media (max-width:760px) { " + spines + ".book-card[data-reading] { width: 112px; height: 168px; margin-top: 56px;", css)

    def test_spine_depth_plank_theme_and_fixed_row_pitches(self):
        css = (_ROOT / "static/css/next.css").read_text(encoding="utf-8")
        self.assertIn('--shelf-pitch: 282px;', css)
        self.assertIn('--plank-back: #1E2430; --plank-top: #303949;', css)
        self.assertIn('--plank-back: #B9955F; --plank-top: #D8BA8A;', css)
        self.assertIn('--shelf-wall-shade: rgba(40,32,24,.07);', css)
        self.assertIn(':root[data-theme="light"] .library-page[data-view="spines"] {', css)
        self.assertIn('.library-page[data-view="spines"] .book-grid {', css)
        self.assertIn('gap: 58px 4px; padding: 14px 18px 50px;', css)
        self.assertIn('background-size: 100% var(--shelf-pitch);', css)
        self.assertIn('background-origin: content-box;', css)
        self.assertIn('margin-top: calc(224px - var(--spine-h, 214px));', css)
        self.assertNotIn('bottom: -9px; height: 5px; background: var(--line-2)', css)

    def test_spine_depth_contact_shadows_without_blur(self):
        css = (_ROOT / "static/css/next.css").read_text(encoding="utf-8")
        self.assertIn('top: 100%; height: 7px; background: linear-gradient(to bottom, var(--shelf-shadow), transparent);', css)
        self.assertNotIn('filter: blur(1.5px)', css)

    def test_spine_depth_overlay_is_above_cover_below_titles(self):
        css = (_ROOT / "static/css/next.css").read_text(encoding="utf-8")
        self.assertIn('.book-card:not([data-reading]) .book-open::after {', css)
        self.assertIn('position: absolute; inset: 0; z-index: 1; pointer-events: none;', css)
        self.assertIn('rgba(255,255,255,.17) 40%', css)
        self.assertIn('.book-copy, .library-page[data-view="spines"] .spine-progress { position: relative; z-index: 4; }', css)
        self.assertIn('.book-card:not([data-reading]) .cover-wrap { display: block; position: absolute; inset: 0; z-index: 0;', css)
        self.assertIn('.book-card:has(.cover-image:not([hidden])) .book-open { color: #f6f1e7; text-shadow:', css)
        self.assertIn('.book-card:hover, .library-page[data-view="spines"] .book-card:focus-within { transform: translateY(-8px);', css)

    def test_spine_depth_pages_open_at_top_with_cover_sides(self):
        css = (_ROOT / "static/css/next.css").read_text(encoding="utf-8")
        self.assertIn('.book-card[data-page-top]:not([data-reading]) .book-open::before {', css)
        self.assertIn('top: 0; left: 5px; right: 5px; height: 20px;', css)
        self.assertIn('repeating-linear-gradient(90deg, var(--book-paper) 0 2px, var(--book-paper-line) 2px 3px)', css)
        self.assertIn('.book-card[data-page-top]:not([data-reading]) .book-open { padding-top: 30px; }', css)
        self.assertIn('.book-card:not([data-reading]) .book-open::before { display: none; }', css)

    def test_spine_depth_page_top_threshold_uses_real_width(self):
        from src import web_server
        original = web_server._build_dashboard_mappings

        def mappings(*args, **kwargs):
            rows, progress = original(*args, **kwargs)
            for row in rows:
                if row["abs_id"] == "library-book":
                    row.update(storygraph_pages=360, hardcover_pages=None)
                elif row["abs_id"] == "library-finished":
                    row.update(storygraph_pages=320, hardcover_pages=None)
            return rows, progress

        self.service.set_user_credential(self.reader.id, "UI_MODE", "new")
        self._login("ui-reader")
        with patch.object(web_server, "_build_dashboard_mappings", side_effect=mappings):
            html = self.client.get("/").get_data(as_text=True)
        # A 44px spine has exposed page paper; a 42px spine does not.
        wide = re.search(r'<article[^>]*data-abs-id="library-book"[^>]*>', html).group()
        narrow = re.search(r'<article[^>]*data-abs-id="library-finished"[^>]*>', html).group()
        self.assertIn('--spine-w:44px;', wide)
        self.assertIn(' data-page-top ', wide)
        self.assertIn('--spine-w:42px;', narrow)
        self.assertNotIn('data-page-top', narrow)
        self.assertIn('spine_w >= 44', (_ROOT / 'templates/next/index.html').read_text(encoding='utf-8'))
        self.assertNotIn('data-page-top', (_ROOT / "templates/index.html").read_text(encoding="utf-8"))

    def test_spine_depth_face_out_wrap_and_page_edge(self):
        css = (_ROOT / "static/css/next.css").read_text(encoding="utf-8")
        self.assertIn('.book-card[data-reading] { width: 142px; height: 214px; margin-top: 10px; margin-inline: 12px 16px;', css)
        self.assertIn('top: -1px; left: 10px; right: -1px; height: 8px;', css)
        self.assertIn('repeating-linear-gradient(0deg, var(--book-paper) 0 1px, var(--book-paper-line) 1px 2px)', css)
        self.assertIn('.book-card[data-reading] .cover-wrap::after {', css)
        self.assertIn('inset: 0 auto 0 0; width: 10px;', css)
        self.assertIn('.book-card[data-reading] .cover-image { object-fit: cover; }', css)

    def test_spine_depth_face_out_page_edge_wins_css_specificity(self):
        """A loaded face-out cover must not hide its paper-top pseudo-element."""
        css = (_ROOT / "static/css/next.css").read_text(encoding="utf-8")
        rules = []
        for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
            display = re.search(r"\bdisplay\s*:\s*(block|none)\s*;", match.group(2))
            if not display:
                continue
            for selector in match.group(1).split(","):
                selector = selector.strip()
                if ('.library-page[data-view="spines"]' in selector
                        and '.book-card' in selector
                        and '.book-open' not in selector
                        and selector.endswith("::before")):
                    rules.append((selector, display.group(1)))

        def before_display(*, reading, cover_loaded):
            applicable = []
            for order, (selector, display) in enumerate(rules):
                if ':has(.cover-image:not([hidden]))' in selector and not cover_loaded:
                    continue
                if ':not([data-reading])' in selector and reading:
                    continue
                if ('[data-reading]' in selector
                        and ':not([data-reading])' not in selector
                        and not reading):
                    continue
                # :not() and :has() contribute their argument specificity, not
                # the functional pseudo-class itself.
                simplified = selector.replace(':has(', '(').replace(':not(', '(')
                spec = (len(re.findall(r'#[\w-]+', simplified)),
                        len(re.findall(r'\.[\w-]+|\[[^]]+\]', simplified)))
                applicable.append((spec, order, display))
            return max(applicable)[-1] if applicable else None

        # With the old :has() rule, specificity beats [data-reading]::before
        # and this first assertion fails: it computes "none" rather than "block".
        self.assertEqual(before_display(reading=True, cover_loaded=True), "block")
        self.assertEqual(before_display(reading=True, cover_loaded=False), "block")
        self.assertEqual(before_display(reading=False, cover_loaded=True), "none")
        self.assertIsNone(before_display(reading=False, cover_loaded=False))

    def test_covers_band_colour_and_resting_sync_cue(self):
        css = (_ROOT / "static/css/next.css").read_text(encoding="utf-8")
        # The letterbox bands take the book's sampled colour, grey until it is known.
        self.assertIn(".cover-wrap { position: relative; display: grid; height: 250px; place-items: center; overflow: hidden; background: var(--spine, var(--surface-2)); }", css)
        hover_block = re.search(r"@media \(hover: hover\) \{(.*?)\n\}", css, re.S).group(1)
        # At rest on a pointer device the bar goes and only the last-synced and out-of-sync icons stay.
        self.assertIn('html:not([data-input="touch"]) .library-book-card:not(:hover):not(:focus-within) .format-strip { background: transparent; box-shadow: none;', hover_block)
        self.assertIn('html:not([data-input="touch"]) .library-book-card:not(:hover):not(:focus-within) .format-slot:not(.is-last):not(.is-oos) { opacity: 0; }', hover_block)
        # Without the bar, the two resting icons get a small dark backing so they read on pale or busy covers.
        self.assertIn(':not(:focus-within) :is(.format-slot.is-last, .format-slot.is-oos) { background-color: rgba(11,13,18,.62); }', hover_block)
        self.assertNotIn(".format-strip { opacity: 0;", css)

    def test_covers_paint_lazily_and_spines_force_covers_to_load(self):
        import shutil
        import subprocess

        node = shutil.which("node")
        if not node:
            self.skipTest("Node is required to execute the cover painter")
        result = subprocess.run(
            [node, str(_ROOT / "tests/test_ui_new_library_paint.js")],
            capture_output=True, text=True, timeout=15, cwd=_ROOT,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        scripts = (_ROOT / "templates/next/_library_scripts.html").read_text(encoding="utf-8")
        self.assertIn("paintSpines(initialLibraryView === 'spines');", scripts)

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
        self.assertNotIn('class="view-switch"', toolbar)
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
        self.assertIn("if (initialLibraryView === 'spines') setBookSelectMode(false)", scripts)
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

    def test_library_filter_preserves_global_search_and_discover_handoff(self):
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

    def test_library_restores_local_filter_alongside_global_ctrl_k_search(self):
        self._login("ui-reader")
        self.service.set_user_credential(self.reader.id, "UI_MODE", "new")
        text = self.client.get("/").get_data(as_text=True)
        topbar = text.split('<header class="topbar">', 1)[1].split('</header>', 1)[0]
        self.assertEqual(text.count('id="bb-search-open"'), 1)
        self.assertIn('id="bb-search-open"', topbar)
        self.assertIn('aria-controls="bb-search-dialog"', topbar)
        self.assertIn('aria-keyshortcuts="Control+K Meta+K /"', topbar)
        self.assertEqual(text.count('id="bb-search-input"'), 1)
        self.assertIn('id="dashboard-search"', text)
        self.assertIn('aria-label="Filter books"', text)
        self.assertIn('placeholder="Filter books…"', text)
        self.assertIn('id="search-handoff"', text)
        self.assertIn('id="search-handoff-link"', text)
        self.assertNotIn('data-library-search-slot', text)
        self.assertEqual(text.count('type="search"'), 2)
        stats = self.client.get("/stats").get_data(as_text=True)
        self.assertIn('id="bb-search-open"', stats)
        self.assertIn('id="bb-search-input"', stats)

    def test_library_header_renders_count_actionable_notices_and_two_control_rows(self):
        from types import SimpleNamespace
        from src import web_server

        self.service.set_user_credential(self.reader.id, "UI_MODE", "new")
        self._login("ui-reader")
        original = web_server._build_dashboard_mappings

        def with_attention(*args, **kwargs):
            mappings, progress = original(*args, **kwargs)
            next(m for m in mappings if m["abs_id"] == "library-book")["status"] = "error"
            return mappings, progress

        suggestion = SimpleNamespace(
            source_id="test-suggestion", title="Match Candidate", author="Example Author",
            audiobook_count=1, origin="shelf_watch", matches=[{"id": "match"}],
        )
        with patch.object(web_server, "_build_dashboard_mappings", side_effect=with_attention), \
             patch.object(web_server.database_service, "get_all_pending_suggestions", return_value=[suggestion]):
            text = self.client.get("/").get_data(as_text=True)

        header = text.split('<header class="page-head">', 1)[1].split("</header>", 1)[0]
        toolbar = text.split('<section class="library-toolbar"', 1)[1].split("</section>", 1)[0]
        self.assertIn("2 books", header)
        self.assertNotIn("YOUR LIBRARY", header)
        self.assertRegex(header, r'<button[^>]*id="suggestions-banner"[^>]*aria-expanded="false"')
        self.assertIn('aria-controls="suggestions-content"', header)
        self.assertIn("Matches to review", header)
        self.assertIn('<details id="needs-attention">', header)
        self.assertIn('id="attention-only-filter"', header)
        self.assertIn("Library Example", header)
        self.assertIn("Alignment failed", header)
        self.assertIn('id="suggestions-section" class="matches-banner" hidden', text)
        self.assertIn('id="suggestion-test-suggestion"', text)
        self.assertIn("dismissSuggestion(event", text)
        self.assertIn("review=test-suggestion", text)
        self.assertLess(toolbar.index('id="filter-select"'), toolbar.index('id="author-filter"'))
        self.assertLess(toolbar.index('id="author-filter"'), toolbar.index('id="series-filter"'))
        self.assertLess(toolbar.index('class="toolbar-primary"'), toolbar.index('class="toolbar-secondary"'))
        self.assertLess(toolbar.index('id="sort-select"'), toolbar.index('id="select-mode-button"'))
        self.assertIn('id="dashboard-search"', text)
        self.assertIn('aria-label="Filter books"', text)
        self.assertIn('aria-label="Author"', text)
        self.assertIn('aria-label="Series"', text)
        self.assertIn('aria-label="Sort"', text)
        css = (_ROOT / "static/css/next.css").read_text(encoding="utf-8")
        self.assertIn("grid-template-columns: repeat(3, minmax(0, 1fr)) minmax(230px, 1.7fr)", css)
        self.assertIn(".library-toolbar .toolbar-search { grid-column: 1 / -1; grid-row: 1; }", css)
        self.assertIn(".library-toolbar .sort-controls .field { min-width: 112px; }", css)
        self.assertNotIn(".view-switch", css)

    def test_tablet_grouping_control_shares_row_and_phone_label_wraps_at_12px(self):
        css = (_ROOT / "static/css/next.css").read_text(encoding="utf-8")
        self.assertIn("@media (min-width:761px) and (max-width:980px)", css)
        self.assertIn(".library-toolbar .toolbar-left, .library-toolbar .toolbar-right { display: contents; }", css)
        self.assertIn(".library-toolbar .group-toggle { grid-column: 1; grid-row: 2; justify-self: start; }", css)
        self.assertIn(".library-toolbar #select-mode-button { grid-column: 3; grid-row: 2; }", css)
        self.assertIn(".library-toolbar #select-mode-button { grid-column: 3; grid-row: 2; }", css)
        self.assertIn("font-size: 12px; line-height: 1.2; white-space: normal;", css)
        self.assertNotIn(".library-toolbar .group-toggle { font-size: 10px; }", css)

    def test_processing_only_books_have_neutral_link_and_no_attention_control(self):
        from src import web_server

        self.service.set_user_credential(self.reader.id, "UI_MODE", "new")
        self._login("ui-reader")
        original = web_server._build_dashboard_mappings

        def processing_only(*args, **kwargs):
            mappings, progress = original(*args, **kwargs)
            for mapping, status in zip(mappings, ("processing", "pending")):
                mapping["status"] = status
                mapping["is_out_of_sync"] = False
            return mappings, progress

        with patch.object(web_server, "_build_dashboard_mappings", side_effect=processing_only):
            text = self.client.get("/").get_data(as_text=True)

        header = text.split('<header class="page-head">', 1)[1].split("</header>", 1)[0]
        self.assertNotIn('id="needs-attention"', header)
        self.assertNotIn('id="attention-only-filter"', header)
        self.assertRegex(header, r'<a class="meta-link processing-meta" href="/job-queue">Processing <span class="count">2</span></a>')
        self.assertEqual(header.count('class="book-count"'), 1)

    def test_attention_and_processing_controls_count_disjoint_books(self):
        from src import web_server

        self.service.set_user_credential(self.reader.id, "UI_MODE", "new")
        self._login("ui-reader")
        original = web_server._build_dashboard_mappings

        def mixed(*args, **kwargs):
            mappings, progress = original(*args, **kwargs)
            mappings[0]["status"] = "failed"
            mappings[1]["status"] = "forging"
            mappings[0]["is_out_of_sync"] = False
            mappings[1]["is_out_of_sync"] = False
            return mappings, progress

        with patch.object(web_server, "_build_dashboard_mappings", side_effect=mixed):
            text = self.client.get("/").get_data(as_text=True)

        header = text.split('<header class="page-head">', 1)[1].split("</header>", 1)[0]
        self.assertIn('href="/job-queue">Processing <span class="count">1</span>', header)
        self.assertIn('Needs attention', header)
        self.assertRegex(header, r'Needs attention\s*<span class="count">1</span>')
        warning = header.split('<details id="needs-attention">', 1)[1]
        self.assertIn("Library Example", warning)
        self.assertNotIn("Finished Example", warning)
        self.assertIn("Alignment failed", warning)

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
                'id="library-page"', 'id="bb-search-open"',
                'id="filter-select"', 'id="series-filter"', 'id="author-filter"',
                'id="sort-select"', 'id="sort-direction"',
                'id="series-grouping-toggle"', 'id="select-mode-button"',
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
        self.assertIn('html:not([data-input="touch"]) .library-book-card:not(:hover):not(:focus-within) .format-strip {', hover_block)
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
        # A reading book stands face-out in Spines, so its spine foot shows nothing.
        self.assertEqual(shown(reading), ["progress-percent", "progress-track"])
        self.assertIn('class="progress-percent" >47%</span>', reading)
        self.assertIn('style="width:47.6%"', reading)
        # Progress sits on the cover; under it come the title then the author.
        self.assertRegex(reading, r'(?s)class="cover-wrap">.*class="progress-percent".*</div>\s*<div class="book-copy"><div class="book-heading"><h3>.*?</h3><span class="book-byline"><span class="book-author">')
        self.assertNotIn("progress-line", reading)

        finished = card("library-finished")
        self.assertEqual(shown(finished), ["progress-done", "spine-progress"])
        self.assertIn('class="progress-done" role="img" aria-label="Finished"', finished)
        # A finished spine's foot carries the gold tick, never a "100%".
        self.assertRegex(finished, r'class="spine-progress"\s+role="img" aria-label="Finished"><svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3.5 8.5l3 3 6-7"/></svg></span>')
        self.assertNotRegex(finished, r'class="spine-progress"[^>]*>[^<]*%')
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
            base = next(m for m in result if m["abs_id"] == "library-book")
            result.append({**base, "abs_id": "unrated", "display_title": "Unrated", "storygraph_rating": None, "goodreads_rating": None})
            return result, progress

        self.service.set_user_credential(self.reader.id, "UI_MODE", "new")
        self._login("ui-reader")
        with patch.object(web_server, "_build_dashboard_mappings", side_effect=mappings):
            text = self.client.get("/").get_data(as_text=True)
        both = re.search(r'<article[^>]*data-abs-id="library-book"[^>]*>.*?</article>', text, re.S).group()
        gr_only = re.search(r'<article[^>]*data-abs-id="library-finished"[^>]*>.*?</article>', text, re.S).group()
        unrated = re.search(r'<article[^>]*data-abs-id="unrated"[^>]*>.*?</article>', text, re.S).group()
        self.assertIn('data-rating="3.5"', both)
        # One averaged figure at the end of the author line; the per-source figures are its tooltip.
        self.assertRegex(both, r'class="book-heading"><h3>.*?</h3><span class="book-byline"><span class="book-author">[^<]*</span><span class="book-ratings" role="img" title="SG 4.0 · GR 3.0" aria-label="Book ratings: SG 4.0 · GR 3.0">★ 3.5</span></span>')
        # CTC and read-along badges live in the book drawer, not on the card.
        self.assertNotIn("cover-badge", both)
        self.assertNotIn("ctc-alignment-badge", both)
        self.assertIn('data-rating="4.4"', gr_only)
        self.assertIn('title="GR 4.4" aria-label="Book ratings: GR 4.4">★ 4.4</span>', gr_only)
        self.assertNotIn("SG ", gr_only)
        self.assertRegex(unrated, r'<span class="book-byline"><span class="book-author">[^<]*</span></span>')
        self.assertNotIn("book-ratings", unrated)
        css = (_ROOT / "static/css/next.css").read_text(encoding="utf-8")
        # A long author truncates instead of pushing the rating off the line.
        self.assertIn(".book-author { display: block; flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;", css)
        self.assertIn(".book-ratings { flex: none;", css)

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

        self.assertNotIn('data-format=', page)
        self.assertIn('id="dashboard-search"', page)
        self.assertIn('id="search-handoff-link"', page)
        self.assertIn('id="attention-only-filter"', page)
        self.assertNotIn('<option value="needs_attention"', page)
        for value in ("all", "paired", "has_audio", "audiobook_only", "ebook_only", "title", "author", "series", "progress", "status", "last_sync", "added", "rating"):
            self.assertIn(f'<option value="{value}"', page)
        self.assertIn("savedFormat === 'needs_attention'", scripts)
        self.assertIn('needsAttention(card)', scripts)
        for label in (
            "Title", "Author", "Series", "Progress", "Status",
            "Last Synced", "Date Added", "Rating",
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
