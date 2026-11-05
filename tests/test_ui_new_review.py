"""Regressions for the P9 review of the New interface (and the Classic code it copies)."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


class TestP9ReviewFixes(unittest.TestCase):
    def test_readalong_status_and_errors_render_as_text_not_html(self):
        # Error text from a failed job (it can quote a filename) must never be parsed as markup.
        for path in ("templates/index.html", "templates/next/_book_scripts.html"):
            text = source(path)
            with self.subTest(path=path):
                self.assertNotRegex(text, r"btn\.innerHTML = '(❌|⏳) ' \+ (stagePrefix|label|\(data\.error)")
                self.assertEqual(text.count("btn.textContent = '❌ '"), 4)

    def test_has_audio_filter_is_a_visible_chip_like_classics_option(self):
        page = source("templates/next/index.html")
        chip = re.search(r'<button[^>]*data-format="has_audio"[^>]*>', page).group()
        self.assertNotIn("hidden", chip)
        self.assertNotIn("data-legacy-format", page + source("templates/next/_library_scripts.html"))

    def test_edition_picker_styles_the_classes_the_shared_builders_create(self):
        css = source("static/css/next.css")
        for builder in ("static/js/hardcover-modal.js", "static/js/storygraph-modal.js"):
            self.assertIn("'hc-edition-option'", source(builder).replace('"', "'"))
        self.assertIn(".hc-edition-option.selected {", css)
        self.assertIn(".hc-edition-detail svg { width: 12px; height: 12px; }", css)
        self.assertNotIn(".sg-edition", css)

    def test_ebook_selection_clears_after_queueing_and_drops_hidden_books(self):
        scripts = source("templates/next/_discover_scripts.html").split("// P4-only additions.", 1)[1]
        # The queue keys ebook-only books as ebook:<filename>; ebook_key never matched it.
        self.assertNotIn("queued.has('ebook:' + key)", scripts)
        self.assertIn("queued.has('ebook:' + filenames.get(key))", scripts)
        self.assertIn("hiddenKeys.ebook_only.has(entry.suggestion_key)) ebookSelectedKeys.delete(entry.ebook_key)", scripts)

    def test_library_refresh_updates_each_apps_position(self):
        page = source("templates/next/index.html")
        scripts = source("templates/next/_library_scripts.html")
        self.assertIn('<span data-app-pct="{{ name }}">', page)
        self.assertIn("card.querySelectorAll('[data-app-pct]')", scripts)

    def test_discover_starts_when_the_browser_blocks_storage(self):
        for path in ("templates/suggestions.html", "templates/next/_discover_scripts.html"):
            text = source(path)
            with self.subTest(path=path):
                self.assertNotRegex(text, r"(readJson|writeJson)\((sessionStorage|localStorage),")
                self.assertIn("try { return window.sessionStorage; } catch (e) { return null; }", text)

    def test_day_discover_actions_are_readable(self):
        css = source("static/css/next-finish.css")
        self.assertIn(':root[data-theme="light"] .discover-page .btn-dismiss {', css)
        self.assertIn(':root[data-theme="light"] .discover-page .btn-reject {', css)

    def test_new_tab_links_carry_rel(self):
        for path in ("templates/admin_user_integrations.html", "templates/next/_admin_user_integrations_scripts.html",
                     "templates/settings.html", "templates/next/settings.html"):
            with self.subTest(path=path):
                self.assertNotRegex(source(path), r'target="_blank" style=')


if __name__ == "__main__":
    unittest.main()
