"""Shelfmark is no longer embedded or in the nav; /shelfmark redirects to the configured site."""
import os
import unittest
from pathlib import Path

from tests import test_ui_new_foundation as foundation

ROOT = Path(__file__).resolve().parents[1]


class TestShelfmarkRedirectOnly(unittest.TestCase):
    setUp = foundation.TestUiModeRoutes.setUp
    tearDown = foundation.TestUiModeRoutes.tearDown
    _login = foundation.TestUiModeRoutes._login

    def _configure(self, url="shelfmark.example"):
        os.environ["SHELFMARK_URL"] = url
        os.environ["SHELFMARK_ENABLED"] = "true"

    def test_nav_has_no_shelfmark_item(self):
        for name in ("templates/next/base.html", "templates/_nav.html"):
            with self.subTest(template=name):
                self.assertNotIn("/shelfmark", (ROOT / name).read_text(encoding="utf-8"))
        self.assertFalse((ROOT / "templates/next/shelfmark.html").exists())
        self._configure()
        self._login("ui-reader")
        for mode in ("classic", "new"):
            with self.subTest(mode=mode):
                self.service.set_user_credential(self.reader.id, "UI_MODE", mode)
                self.assertNotIn('href="/shelfmark"', self.client.get("/stats").get_data(as_text=True))

    def test_shelfmark_route_redirects_in_both_interfaces(self):
        self._login("ui-reader")
        for mode in ("classic", "new"):
            for configured, target in (("shelfmark.example", "http://shelfmark.example"),
                                       ("https://shelfmark.example/x", "https://shelfmark.example/x")):
                with self.subTest(mode=mode, url=configured):
                    self._configure(configured)
                    self.service.set_user_credential(self.reader.id, "UI_MODE", mode)
                    response = self.client.get("/shelfmark")
                    self.assertEqual(response.status_code, 302)
                    self.assertEqual(response.headers["Location"], target)

    def test_unconfigured_shelfmark_returns_to_the_library(self):
        self._configure("")
        self._login("ui-reader")
        for mode in ("classic", "new"):
            with self.subTest(mode=mode):
                self.service.set_user_credential(self.reader.id, "UI_MODE", mode)
                response = self.client.get("/shelfmark")
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response.headers["Location"].endswith("/"))

    def test_settings_offer_open_shelfmark_when_configured(self):
        text = (ROOT / "templates/next/settings.html").read_text(encoding="utf-8")
        self.assertIn("{% if get_val('SHELFMARK_URL') %}", text)
        self.assertIn('href="/shelfmark" target="_blank" rel="noopener noreferrer"', text)
        self.assertNotIn("a Shelfmark tab appears", text)


if __name__ == "__main__":
    unittest.main()
