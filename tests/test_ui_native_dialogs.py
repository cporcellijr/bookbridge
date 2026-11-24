"""Modal dialogs use native <dialog>.showModal(), which keeps keyboard focus inside them.

The 2026-11-17 audit found the Stats day/book detail, Discover Forge staging and Get-a-book
modals declared aria-modal but let Tab move to the page behind them, never moved focus in
(Stats) and never returned it. A modal <dialog> moves focus in, makes the page inert,
closes on Escape and returns focus to the opener.
"""

from pathlib import Path
import re
import unittest

_ROOT = Path(__file__).resolve().parent.parent


def _read(path: str) -> str:
    return (_ROOT / path).read_text(encoding="utf-8")


class TestNativeModalDialogs(unittest.TestCase):
    def test_modal_markup_is_a_dialog_in_both_interfaces(self):
        cases = {
            "templates/stats.html": ["day-detail-backdrop", "book-detail-backdrop"],
            "templates/next/stats.html": ["day-detail-backdrop", "book-detail-backdrop"],
            "templates/suggestions.html": ["forgeStageModal"],
            "templates/next/suggestions.html": ["forgeStageModal"],
            "templates/_acquisition_intake.html": ["acqModal"],
        }
        for path, ids in cases.items():
            source = _read(path)
            self.assertNotIn('aria-modal="true"', source, path)
            for element_id in ids:
                with self.subTest(path=path, id=element_id):
                    self.assertRegex(source, rf'<dialog class="[^"]+" id="{element_id}" aria-labelledby="[^"]+">')

    def test_scripts_open_with_show_modal_and_close_natively(self):
        stats = _read("templates/next/_stats_scripts.html")
        self.assertEqual(stats.count(".showModal()"), 2)
        self.assertNotIn('"is-open"', stats)
        forge = _read("templates/next/_discover_main_script.html")
        self.assertIn("forgeStageModal.showModal()", forge)
        self.assertNotIn("forgeStageModal.classList", forge)
        acquisition = _read("templates/next/_acquisition_scripts.html")
        self.assertIn("$('acqModal').showModal()", acquisition)
        self.assertNotIn("$('acqModal').classList", acquisition)
        # Escape runs each modal's own close path so its cleanup still happens.
        for script in (forge, acquisition):
            self.assertRegex(script, r"addEventListener\('cancel', \(event\) => \{\s*event\.preventDefault\(\);")

    def test_dialog_overlays_keep_their_full_screen_layout(self):
        for path in ("static/css/app.css", "static/css/next-discover.css"):
            css = _read(path)
            with self.subTest(path=path):
                self.assertIn("dialog.modal-overlay { width: auto; height: auto; max-width: none; max-height: none;", css)
                self.assertIn("dialog.modal-overlay::backdrop { background: transparent; }", css)
        for path in ("templates/stats.html", "templates/next/stats.html"):
            css = _read(path)
            with self.subTest(path=path):
                self.assertIn(".day-detail-backdrop[open] { display: flex; }", css)
                self.assertTrue(re.search(r"\.day-detail-backdrop \{[^}]*max-width: none;", css))


if __name__ == "__main__":
    unittest.main()
