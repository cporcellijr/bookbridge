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
        self.assertIn('aria-label="{{ _(\'Shelfmark Tool\') }}"', text)

    def test_settings_sections_have_nine_distinct_icons(self):
        markup = source("templates/next/settings.html")
        sections = re.findall(
            r'<button[^>]+class="settings-tab[^"]*"[^>]+data-tab="([^"]+)"[^>]*>'
            r'<svg[^>]*><use href="/static/icons.svg#([^"]+)"',
            markup,
        )
        self.assertEqual(len(sections), 9)
        self.assertEqual(len({icon for _name, icon in sections}), 9)
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
        self.assertIn('id="library-page" class="library-page" data-view="covers"', library)
        self.assertIn("setTimeout(refreshDashboard, 30000)", source("templates/next/_library_scripts.html"))
        self.assertIn('<label class="book-select-hit">', library)

    def test_view_switch_only_binds_its_own_buttons_not_the_library_page(self):
        # #library-page carries data-view too; a bare [data-view] selector made every
        # click in the Library clear the switch's pressed state and regroup series.
        scripts = source("templates/next/_library_scripts.html").split("/* P3 additions:", 1)[1]
        self.assertNotIn("querySelectorAll('[data-view]')", scripts)
        self.assertNotIn("querySelector('[data-view=", scripts)
        self.assertEqual(scripts.count(".view-switch [data-view"), 4)

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

    def test_docs_and_unreleased_entry_identify_beta_switch(self):
        changelog = source("CHANGELOG.md").split("## [Unreleased]", 1)[1].split("### Fixed", 1)[0]
        self.assertIn("**The New interface (beta)", changelog)
        self.assertIn("My Integrations → Reading preferences → Interface", changelog)
        self.assertIn("Stats is now open to every reader", changelog)
        self.assertIn("new-interface.md", source("mkdocs.yml"))
        self.assertTrue((ROOT / "docs/new-interface.md").is_file())


if __name__ == "__main__":
    unittest.main()
