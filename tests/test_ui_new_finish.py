"""P8 finish regressions: New-only layout, navigation, accessibility and theme."""
from __future__ import annotations

import re
from pathlib import Path
import unittest

from flask.signals import template_rendered

from tests.test_ui_new_foundation import TestUiModeRoutes

ROOT = Path(__file__).resolve().parent.parent


def source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def contrast(foreground: str, background: str) -> float:
    """WCAG 2.x contrast for opaque hexadecimal colors."""
    def luminance(value: str) -> float:
        channels = [int(value[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        linear = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in channels]
        return sum(v * k for v, k in zip(linear, (0.2126, 0.7152, 0.0722)))
    a, b = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (a + 0.05) / (b + 0.05)


class TestP8Finish(unittest.TestCase):
    setUp = TestUiModeRoutes.setUp
    tearDown = TestUiModeRoutes.tearDown
    _login = TestUiModeRoutes._login

    def test_classic_routes_still_use_classic_templates_by_default(self):
        observed = []

        def capture(_sender, template, **_kwargs):
            observed.append(template.name)

        template_rendered.connect(capture, self.app)
        try:
            self._login("ui-reader")
            for path, name in (("/", "index.html"), ("/suggestions", "suggestions.html"),
                               ("/stats", "stats.html"), ("/account", "account.html"),
                               ("/account/integrations", "account_integrations.html"),
                               ("/job-queue", "job_queue.html"), ("/whats-new", "whats_new.html")):
                with self.subTest(path=path):
                    observed.clear()
                    response = self.client.get(path)
                    self.assertEqual(response.status_code, 200)
                    self.assertIn(name, observed)
                    self.assertNotIn("next/" + name, observed)
        finally:
            template_rendered.disconnect(capture, self.app)

    def test_new_shell_has_short_named_app_links_with_accessible_full_name(self):
        text = source("templates/next/base.html")
        self.assertIn("title=\"{{ title }}\" aria-label=\"{{ title }}\"", text)
        self.assertIn("<span>{{ short_name }}</span>", text)
        for name in ("Audiobookshelf", "Calibre-Web", "Grimmory", "BookOrbit", "Kavita"):
            self.assertIn(f"_('{name}')", text)

    def test_settings_categories_keep_distinct_icons_after_p11_navigation(self):
        markup = source("templates/next/settings.html")
        sections = re.findall(
            r'<a[^>]+class="settings-tab"[^>]+data-tab="([^"]+)"[^>]*>'
            r'<svg[^>]*><use href="/static/icons.svg#([^"]+)"',
            markup,
        )
        self.assertEqual([name for name, _icon in sections],
                         ['sync', 'library', 'alignment', 'ai', 'system',
                          'maintenance', 'users', 'logs'])
        self.assertEqual(len({icon for _name, icon in sections}), 8)
        symbols = set(re.findall(r'<symbol id="([^"]+)"', source("static/icons.svg")))
        self.assertTrue(set(icon for _name, icon in sections).issubset(symbols))

    def test_masonry_uses_classic_grid_packing_and_survives_updates(self):
        js = source("static/js/next/masonry.js")
        css = source("static/css/next-finish.css")
        library = source("templates/next/index.html")
        for required in ("ResizeObserver", "MutationObserver", "requestAnimationFrame",
                         "gridRowEnd", "getBoundingClientRect().height", "window.bbLayoutMasonry",
                         "classList.toggle('masonry', covers)", "document.addEventListener('change'",
                         "document.addEventListener('click'"):
            self.assertIn(required, js)
        self.assertIn("grid-auto-rows: 4px", css)
        self.assertIn("row-gap: 0", css)
        self.assertIn('src="/static/js/next/masonry.js"', library)
        self.assertIn('data-view="{{', library)
        self.assertIn("bbPoller({ name: 'library-progress', fastMs: 30000, slowMs: 30000, immediate: false, run: refreshDashboard })", source("templates/next/_library_scripts.html"))
        self.assertIn('<label class="book-select-hit">', library)

    def test_view_switch_is_removed_without_changing_the_server_view(self):
        scripts = source("templates/next/_library_scripts.html")
        markup = source("templates/next/index.html")
        self.assertNotIn(".view-switch", scripts + markup)
        self.assertNotIn("abs_kosync_view", scripts)
        self.assertIn("initialLibraryView = libraryPage.dataset.view", scripts)

    def test_logs_checkboxes_stay_small_and_messages_get_the_room(self):
        """iPad Safari stretched the Logs checkboxes to the 44px field height and squeezed messages."""
        css = source("static/css/next-ops.css")
        rule = lambda selector: css[css.index(selector + " {"):css.index("}", css.index(selector + " {"))]
        checkbox = rule('.view .logs-wrap .auto-refresh input[type="checkbox"]')
        for declaration in ("min-height: 0", "width: 22px", "height: 22px", "transform: none"):
            self.assertIn(declaration, checkbox)
        self.assertIn("flex-wrap: wrap", rule(".view .logs-wrap .log-line"))
        self.assertIn("word-break: normal", rule(".view .logs-wrap .log-line"))
        self.assertIn("flex: none", rule(".view .logs-wrap .log-timestamp, .view .logs-wrap .log-level, .view .logs-wrap .log-module"))
        self.assertRegex(rule(".view .logs-wrap .log-message"), r"flex: 1 1 \d+px")

    def test_book_cover_column_matches_the_cover_at_every_breakpoint(self):
        """iPad drawer (~710px): the 760px rule shrank the column to 90px while the cover stayed 150px."""
        base, finish = source("static/css/next.css"), source("static/css/next-finish.css")

        def block(css: str, width: int) -> str:
            start = css.index(f"@media (max-width: {width}px) {{")
            return css[start:css.index("\n}", start)]

        self.assertIn(".book-cover { width: 150px;", base)
        self.assertIn(".book-cover { width: 90px;", block(base, 640))
        self.assertIn(".book-hero { grid-template-columns: minmax(0,150px) minmax(0,1fr); }", block(finish, 760))
        self.assertIn(".book-hero { grid-template-columns: minmax(0,90px) minmax(0,1fr); }", block(finish, 640))

    def test_discover_chip_text_is_unchanged_and_visuals_use_new_tokens(self):
        scripts = source("templates/next/_discover_scripts.html")
        css = source("static/css/next-finish.css")
        for word in ("Needs review", "Manual choice", "Same folder", "Same series", "Exact match"):
            self.assertIn(word, scripts)
        self.assertIn(".discover-page .source-badge", css)
        self.assertIn("font: 600 11px/1.35 var(--font-ui)", css)
        self.assertIn("border-radius: 999px", css)

    def test_search_and_drawer_trap_and_restore_focus_on_safari(self):
        search = source("static/js/next/search.js")
        book = source("static/js/next/book.js")
        self.assertIn("element.inert = true", search)
        self.assertIn("element.inert = false", search)
        self.assertIn("event.shiftKey && document.activeElement === first", search)
        self.assertIn("restore.focus()", search)
        self.assertIn("dialog.showModal()", book)
        self.assertIn("doc.activeElement === first", book)
        self.assertIn("getClientRects().length > 0", book)
        self.assertIn("restore.focus(", book)
        self.assertNotIn("checkVisibility()", book)

    def test_day_palette_meets_w_c_a_g_aa_for_common_and_semantic_text(self):
        palette = source("static/css/next-finish.css")
        light = {
            "background": "#F1F2F4",
            "surface": "#FFFFFF",
            "surface2": "#F6F7F9",
            "text": "#14171D",
            "secondary": "#4A5160",
            "muted": "#555E6E",
            "success": "#116942",
            "warning": "#9F420F",
            "error": "#B5263D",
            "info": "#214FA8",
        }
        for key in ("muted", "success", "warning", "error", "info"):
            self.assertIn(light[key], palette)
        for bg in ("background", "surface", "surface2"):
            for fg in ("text", "secondary", "muted", "success", "warning", "error", "info"):
                with self.subTest(fg=fg, bg=bg):
                    self.assertGreaterEqual(contrast(light[fg], light[bg]), 4.5)

    def test_day_theme_overrides_classic_light_on_dark_admin_text(self):
        # Classic-copied admin markup draws translucent white text, which a contrast
        # audit that skips translucent colors never saw: unreadable in Day mode.
        css = source("static/css/next-finish.css")
        day_rules = " ".join(line for line in css.splitlines() if line.startswith(':root[data-theme="light"]'))
        for selector in ('[style*="color: rgba(255, 255, 255"]', '[style*="color:rgba(255,255,255"]',
                         'details.advanced-sub summary', '.tracker-tag', '.logs-wrap .log-module',
                         '.u-badge.admin', '.u-btn-sm', 'td[style="color:#888"]'):
            with self.subTest(selector=selector):
                self.assertIn(selector, day_rules)
        self.assertIn('#diagnostics-optin-modal .hc-modal-footer button:not(.hc-btn-cancel) { background: var(--ribbon)', css)

    def test_docs_and_release_entry_identify_beta_switch(self):
        # The beta switch shipped in 7.13.0; the release moved its entries out of Unreleased.
        changelog = source("CHANGELOG.md").split("## [7.13.0]", 1)[1].split("\n## [", 1)[0]
        self.assertIn("**The New interface (beta)", changelog)
        self.assertIn("New interface", changelog)
        self.assertIn("Classic | New", changelog)
        self.assertIn("without a Save step", changelog)
        self.assertIn("Stats is now open to every reader", changelog)
        self.assertIn("new-interface.md", source("mkdocs.yml"))
        self.assertTrue((ROOT / "docs/new-interface.md").is_file())


if __name__ == "__main__":
    unittest.main()
