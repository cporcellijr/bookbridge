"""Shelfmark opens inside the New interface; Classic keeps the external redirect."""
import os
import unittest

from tests import test_ui_new_foundation as foundation


class TestShelfmarkInApp(unittest.TestCase):
    setUp = foundation.TestUiModeRoutes.setUp
    tearDown = foundation.TestUiModeRoutes.tearDown
    _login = foundation.TestUiModeRoutes._login

    def _configure(self, url="shelfmark.example"):
        os.environ["SHELFMARK_URL"] = url
        os.environ["SHELFMARK_ENABLED"] = "true"

    def test_new_frames_shelfmark_with_a_new_tab_fallback(self):
        self._configure()
        self._login("ui-reader")
        self.service.set_user_credential(self.reader.id, "UI_MODE", "new")
        response = self.client.get("/shelfmark")
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn('<iframe class="shelfmark-frame" src="http://shelfmark.example"', text)
        self.assertIn('href="http://shelfmark.example" target="_blank" rel="noopener noreferrer"', text)
        self.assertIn('id="shelfmark-mixed"', text)
        # The rail link stays in the app and marks the page as current.
        self.assertIn('<a class="app-link" href="/shelfmark" aria-current="page"', text)
        library = self.client.get("/stats").get_data(as_text=True)
        self.assertIn('<a class="app-link" href="/shelfmark" title=', library)
        self.assertNotIn('href="/shelfmark" target="_blank"', library)

    def test_classic_still_redirects_to_shelfmark(self):
        self._configure()
        self._login("ui-reader")
        response = self.client.get("/shelfmark")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "http://shelfmark.example")

    def test_unconfigured_shelfmark_returns_to_the_library_in_both_interfaces(self):
        self._configure("")
        self._login("ui-reader")
        for mode in ("classic", "new"):
            with self.subTest(mode=mode):
                self.service.set_user_credential(self.reader.id, "UI_MODE", mode)
                response = self.client.get("/shelfmark")
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response.headers["Location"].endswith("/"))

    def test_frame_address_is_escaped(self):
        self._configure('x" onload="alert(1)')
        self._login("ui-reader")
        self.service.set_user_credential(self.reader.id, "UI_MODE", "new")
        text = self.client.get("/shelfmark").get_data(as_text=True)
        self.assertNotIn('onload="alert', text)
        self.assertIn('src="http://x&#34; onload=&#34;alert(1)"', text)


if __name__ == "__main__":
    unittest.main()
