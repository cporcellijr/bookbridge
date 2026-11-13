"""P15a: per-reader Display settings for the New interface (store, save route, library attributes)."""

import os
import unittest

from src.utils.config_loader import ALL_SETTINGS, DEFAULT_CONFIG
from src.utils.display_settings import (
    DISPLAY_SETTINGS,
    normalize_display_value,
    resolve_display_settings,
)
from src.utils.user_config import PER_USER_CREDENTIAL_KEYS
from tests.test_ui_new_foundation import TestUiModeRoutes


class TestDisplayValues(unittest.TestCase):
    def test_every_setting_is_registered_with_an_allowed_default(self):
        for key, allowed in DISPLAY_SETTINGS.items():
            with self.subTest(key=key):
                self.assertIn(key, ALL_SETTINGS)
                self.assertIn(DEFAULT_CONFIG[key], allowed)
                # Display keys are not account credentials: no admin seeding, no sync bundles.
                self.assertNotIn(key, PER_USER_CREDENTIAL_KEYS)

    def test_normalize_booleans_choices_and_rejects(self):
        cases = [
            ("UI_SHOW_HERO", "on", "true"), ("UI_SHOW_HERO", "off", "false"), ("UI_SHOW_TITLES", "on", "true"), ("UI_SHOW_TITLES", " TRUE ", "true"),
            ("UI_SHOW_TITLES", "1", "true"), ("UI_SHOW_TITLES", "off", "false"),
            ("UI_SHOW_TITLES", "false", "false"), ("UI_SHOW_TITLES", "maybe", None),
            ("UI_THEME", "Night", "night"), ("UI_THEME", "purple", None),
            ("UI_COVER_SIZE", 3, "3"), ("UI_COVER_SIZE", "6", None),
            ("UI_LIBRARY_STYLE", "faceout", "faceout"), ("UI_MODE", "new", None),
            ("NOT_A_SETTING", "true", None), ("UI_THEME", None, None),
        ]
        for key, raw, expected in cases:
            with self.subTest(key=key, raw=raw):
                self.assertEqual(normalize_display_value(key, raw), expected)

    def test_resolve_prefers_reader_then_server_then_default(self):
        saved = dict(os.environ)
        try:
            os.environ.pop("UI_THEME", None)
            os.environ["UI_COVER_SIZE"] = "4"
            os.environ["UI_SHELF_FINISH"] = "bogus"
            resolved = resolve_display_settings({"UI_THEME": "night", "UI_SHOW_RATINGS": "junk"})
        finally:
            os.environ.clear()
            os.environ.update(saved)
        self.assertEqual(resolved["UI_THEME"], "night")
        self.assertEqual(resolved["UI_COVER_SIZE"], "4")
        self.assertEqual(resolved["UI_SHELF_FINISH"], DEFAULT_CONFIG["UI_SHELF_FINISH"])
        self.assertEqual(resolved["UI_SHOW_RATINGS"], DEFAULT_CONFIG["UI_SHOW_RATINGS"])
        self.assertEqual(set(resolved), set(DISPLAY_SETTINGS))
        self.assertEqual(resolved["UI_SHOW_HERO"], "true")


class TestDisplaySaveRoute(unittest.TestCase):
    setUp = TestUiModeRoutes.setUp
    tearDown = TestUiModeRoutes.tearDown
    _login = TestUiModeRoutes._login

    def _stored(self, user, key):
        return self.service.get_user_credential(user.id, key)

    def test_form_save_writes_only_this_reader(self):
        self._login("ui-reader")
        response = self.client.post(
            "/account/display?next=/account",
            data={"UI_THEME": "day", "UI_COVER_SIZE": "4", "UI_SHOW_TITLES": "on", "csrf_token": "x"},
        )
        self.assertEqual((response.status_code, response.location), (302, "/account"))
        self.assertEqual(self._stored(self.reader, "UI_THEME"), "day")
        self.assertEqual(self._stored(self.reader, "UI_COVER_SIZE"), "4")
        self.assertEqual(self._stored(self.reader, "UI_SHOW_TITLES"), "true")
        self.assertIsNone(self._stored(self.reader, "csrf_token"))
        self.assertIsNone(self._stored(self.admin, "UI_THEME"))

    def test_hero_setting_is_reader_scoped_and_hidden_false_saves(self):
        self._login("ui-reader")
        self.client.post("/account/display", data={"UI_SHOW_HERO": ["false", "on"]})
        self.assertEqual(self._stored(self.reader, "UI_SHOW_HERO"), "true")
        self.assertIsNone(self._stored(self.admin, "UI_SHOW_HERO"))
        self.client.post("/account/display", data={"UI_SHOW_HERO": ["false"]})
        self.assertEqual(self._stored(self.reader, "UI_SHOW_HERO"), "false")

    def test_hidden_false_then_checkbox_takes_the_last_value(self):
        self._login("ui-reader")
        self.client.post("/account/display", data={"UI_SHOW_RATINGS": ["false", "on"]})
        self.assertEqual(self._stored(self.reader, "UI_SHOW_RATINGS"), "true")
        self.client.post("/account/display", data={"UI_SHOW_RATINGS": ["false"]})
        self.assertEqual(self._stored(self.reader, "UI_SHOW_RATINGS"), "false")

    def test_json_save_returns_resolved_settings(self):
        self._login("ui-reader")
        response = self.client.post(
            "/account/display",
            json={"UI_LIBRARY_STYLE": "spines", "SERIES_SHOW_CURRENT_BOOK_DETAIL": True},
            headers={"Accept": "application/json"},
        )
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["settings"]["UI_LIBRARY_STYLE"], "spines")
        self.assertEqual(body["settings"]["SERIES_SHOW_CURRENT_BOOK_DETAIL"], "true")
        self.assertEqual(set(body["settings"]), set(DISPLAY_SETTINGS))

    def test_bad_requests_write_nothing(self):
        self._login("ui-reader")
        self.service.set_user_credential(self.reader.id, "UI_THEME", "night")
        for payload in (
            {"UI_THEME": "day", "UI_MODE": "classic"},
            {"UI_THEME": "day", "UI_COVER_SIZE": "9"},
            {"UI_THEME": "day", "ABS_KEY": "stolen"},
            {},
            {"csrf_token": "x"},
        ):
            with self.subTest(payload=payload):
                response = self.client.post("/account/display", data=payload)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self._stored(self.reader, "UI_THEME"), "night")
        self.assertIsNone(self._stored(self.reader, "UI_COVER_SIZE"))
        self.assertIsNone(self._stored(self.reader, "ABS_KEY"))

    def test_signed_out_post_writes_nothing(self):
        response = self.client.post("/account/display", data={"UI_THEME": "day"})
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.location)
        response = self.client.post(
            "/account/display", json={"UI_THEME": "day"}, headers={"Accept": "application/json"}
        )
        self.assertEqual(response.status_code, 401)
        for user in (self.reader, self.admin):
            self.assertIsNone(self._stored(user, "UI_THEME"))

    def test_library_page_carries_the_readers_settings(self):
        # Use the checked-in templates, not P1's "new home" stand-in.
        self.app.jinja_loader = self.app.jinja_loader.loaders[1]
        self.app.jinja_env.cache.clear()
        self.service.set_user_credential(self.reader.id, "UI_MODE", "new")
        self._login("ui-reader")
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn('data-library-style="cards"', page)
        self.assertIn('data-cover-size="2"', page)
        self.assertIn('data-show-titles="true"', page)

        self.client.post("/account/display", data={
            "UI_LIBRARY_STYLE": "faceout", "UI_COVER_SIZE": "5", "UI_SHELF_FINISH": "wood",
            "UI_SHOW_TITLES": "false", "UI_SHOW_FORMAT_STRIP": "off",
        })
        page = self.client.get("/").get_data(as_text=True)
        for attribute in ('data-library-style="faceout"', 'data-cover-size="5"', 'data-shelf-finish="wood"',
                          'data-show-titles="false"', 'data-show-format-strip="false"',
                          'data-show-progress="true"'):
            self.assertIn(attribute, page)


if __name__ == "__main__":
    unittest.main()
