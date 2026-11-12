"""P15b: Account Display controls and the existing per-reader series view."""

import re
import unittest

from werkzeug.datastructures import MultiDict

from src.api.admin_api import _field_types
from src.db.models import Book
from src.utils.user_config import PER_USER_FIELD_GROUPS, PER_USER_PREFERENCE_KEYS
from tests.test_ui_new_foundation import TestUiModeRoutes


SERIES_KEY = "SERIES_SHOW_CURRENT_BOOK_DETAIL"


class TestAccountDisplayPage(unittest.TestCase):
    setUp = TestUiModeRoutes.setUp
    tearDown = TestUiModeRoutes.tearDown
    _login = TestUiModeRoutes._login

    def _real_templates(self):
        self.app.jinja_loader = self.app.jinja_loader.loaders[1]
        self.app.jinja_env.cache.clear()

    def _series_books(self):
        for index in (1, 2):
            book_id = f"p15b-series-{index}"
            self.service.save_book(Book(
                abs_id=book_id, abs_title=f"Series book {index}", status="active",
                ebook_filename=f"{book_id}.epub", duration=3600,
                series_name="P15b Series", series_sequence=index,
            ))
            self.service.link_user_book(self.reader.id, book_id)

    def test_account_display_shows_interface_in_both_modes_but_series_only_in_new(self):
        self._real_templates()
        self._login("ui-reader")
        for mode in ("classic", "new"):
            with self.subTest(mode=mode):
                self.service.set_user_credential(self.reader.id, "UI_MODE", mode)
                response = self.client.get("/account")
                self.assertEqual(response.status_code, 200)
                markup = response.get_data(as_text=True)
                self.assertIn('id="account-display"', markup)
                form = re.search(
                    r'<form class="account-interface-form"[^>]*>.*?</form>',
                    markup, re.S,
                )
                self.assertIsNotNone(form)
                self.assertIn('name="ui_mode" value="classic"', form.group())
                self.assertIn('name="ui_mode" value="new"', form.group())
                if mode == "new":
                    series_form = re.search(
                        r'<form class="account-series-form"[^>]*>.*?</form>',
                        markup, re.S,
                    )
                    self.assertIsNotNone(series_form)
                    self.assertIn('name="SERIES_SHOW_CURRENT_BOOK_DETAIL" value="false"', series_form.group())
                    self.assertRegex(
                        series_form.group(),
                        r'type="checkbox"[^>]+name="SERIES_SHOW_CURRENT_BOOK_DETAIL" value="true"',
                    )
                    self.assertIn('action="/account/display?next=/account"', series_form.group())
                    theme_form = re.search(
                        r'<form class="account-theme-form"[^>]*>.*?</form>',
                        markup, re.S,
                    )
                    self.assertIsNotNone(theme_form)
                    self.assertIn('name="UI_THEME"', theme_form.group())
                    for value in ("auto", "day", "night"):
                        self.assertIn(f'value="{value}"', theme_form.group())
                    library_form = re.search(
                        r'<form class="account-library-style-form"[^>]*>.*?</form>', markup, re.S,
                    )
                    self.assertIsNotNone(library_form)
                    for style in ("cards", "spines", "list"):
                        self.assertIn(f'value="{style}"', library_form.group())
                    self.assertNotIn('value="faceout"', library_form.group())
                else:
                    self.assertNotIn('account-library-style-form', markup)
                    self.assertNotIn('name="UI_LIBRARY_STYLE"', markup)
                    self.assertNotIn('account-theme-form', markup)
                    self.assertNotIn('name="UI_THEME"', markup)
                    self.assertNotIn('account-series-form', markup)
                    self.assertNotIn('name="SERIES_SHOW_CURRENT_BOOK_DETAIL"', markup)
                for future_setting in (
                    "UI_COVER_SIZE", "UI_SHOW_PROGRESS",
                    "UI_SHOW_FINISHED", "UI_SHOW_FORMAT_STRIP", "UI_SHOW_RATINGS",
                    "UI_SHOW_TITLES", "UI_SHELF_FINISH",
                ):
                    self.assertNotIn(f'name="{future_setting}"', markup)

    def test_library_style_form_saves_only_the_reader_and_reflects_the_choice(self):
        self._real_templates()
        self.service.set_user_credential(self.reader.id, "UI_MODE", "new")
        self._login("ui-reader")
        for preference in ("cards", "spines", "list"):
            with self.subTest(preference=preference):
                response = self.client.post("/account/display?next=/account",
                                            data={"UI_LIBRARY_STYLE": preference},
                                            follow_redirects=True)
                self.assertEqual(response.status_code, 200)
                markup = response.get_data(as_text=True)
                form = re.search(r'<form class="account-library-style-form"[^>]*>.*?</form>',
                                 markup, re.S)
                self.assertIsNotNone(form)
                self.assertRegex(form.group(), rf'value="{preference}"\s+selected')
                self.assertEqual(self.service.get_user_credential(self.reader.id, "UI_LIBRARY_STYLE"), preference)
                self.assertIsNone(self.service.get_user_credential(self.admin.id, "UI_LIBRARY_STYLE"))
        self.client.get("/logout")
        self._login("ui-admin")
        self.service.set_user_credential(self.admin.id, "UI_MODE", "new")
        markup = self.client.get("/account").get_data(as_text=True)
        form = re.search(r'<form class="account-library-style-form"[^>]*>.*?</form>', markup, re.S)
        self.assertRegex(form.group(), r'value="cards"\s+selected')

    def test_theme_form_saves_for_one_reader_and_reflects_the_choice(self):
        self._real_templates()
        self.service.set_user_credential(self.reader.id, "UI_MODE", "new")
        self._login("ui-reader")
        for preference in ("auto", "day", "night"):
            with self.subTest(preference=preference):
                response = self.client.post(
                    "/account/display?next=/account",
                    data={"UI_THEME": preference},
                    follow_redirects=True,
                )
                self.assertEqual(response.status_code, 200)
                markup = response.get_data(as_text=True)
                form = re.search(
                    r'<form class="account-theme-form"[^>]*>.*?</form>',
                    markup, re.S,
                )
                self.assertIsNotNone(form)
                self.assertRegex(form.group(), rf'value="{preference}"\s+selected')
                self.assertIn(f'data-theme-pref="{preference}"', markup)
                self.assertEqual(self.service.get_user_credential(self.reader.id, "UI_THEME"), preference)
                self.assertIsNone(self.service.get_user_credential(self.admin.id, "UI_THEME"))
                self.assertIsNone(self.service.get_user_credential(self.reader.id, SERIES_KEY))

        self.client.get("/logout")
        self._login("ui-admin")
        self.service.set_user_credential(self.admin.id, "UI_MODE", "new")
        other = self.client.get("/account").get_data(as_text=True)
        self.assertIn('data-theme-pref="auto"', other)
        self.assertRegex(other, r'value="auto"\s+selected')

    def test_series_form_saves_only_reader_and_changes_real_library_markup(self):
        self._real_templates()
        self._series_books()
        self.service.set_user_credential(self.reader.id, "UI_MODE", "new")
        self._login("ui-reader")
        account_before = self.client.get("/account").get_data(as_text=True)
        self.assertIn('class="account-series-form"', account_before)
        self.assertIn('name="SERIES_SHOW_CURRENT_BOOK_DETAIL" value="false"', account_before)

        before = self.client.get("/").get_data(as_text=True)
        self.assertIn('data-series-name="P15b Series"', before)
        self.assertNotIn('class="series-strip series-header"', before)

        enabled = self.client.post(
            "/account/display?next=/account",
            data=MultiDict([
                ("SERIES_SHOW_CURRENT_BOOK_DETAIL", "false"),
                ("SERIES_SHOW_CURRENT_BOOK_DETAIL", "true"),
            ]),
        )
        self.assertEqual((enabled.status_code, enabled.location), (302, "/account"))
        self.assertEqual(self.service.get_user_credential(self.reader.id, SERIES_KEY), "true")
        self.assertIsNone(self.service.get_user_credential(self.admin.id, SERIES_KEY))
        account = self.client.get("/account").get_data(as_text=True)
        self.assertRegex(
            account,
            r'name="SERIES_SHOW_CURRENT_BOOK_DETAIL" value="true"\s+checked',
        )
        after = self.client.get("/").get_data(as_text=True)
        self.assertIn('class="series-strip series-header"', after)

        disabled = self.client.post(
            "/account/display?next=/account",
            data={"SERIES_SHOW_CURRENT_BOOK_DETAIL": "false"},
        )
        self.assertEqual((disabled.status_code, disabled.location), (302, "/account"))
        self.assertEqual(self.service.get_user_credential(self.reader.id, SERIES_KEY), "false")
        self.assertNotIn('class="series-strip series-header"', self.client.get("/").get_data(as_text=True))
        self.assertIsNone(self.service.get_user_credential(self.admin.id, SERIES_KEY))

    def test_integrations_and_admin_user_page_no_longer_expose_series(self):
        self._real_templates()
        self._login("ui-reader")
        for mode in ("classic", "new"):
            with self.subTest(mode=mode):
                self.service.set_user_credential(self.reader.id, "UI_MODE", mode)
                response = self.client.get("/account/integrations")
                self.assertEqual(response.status_code, 200)
                self.assertNotIn(f'name="{SERIES_KEY}"', response.get_data(as_text=True))
        self.assertNotIn(SERIES_KEY, _field_types())
        self.assertNotIn(SERIES_KEY, {
            key for _group, fields in PER_USER_FIELD_GROUPS for key, _label, _kind in fields
        })
        self.assertIn(SERIES_KEY, PER_USER_PREFERENCE_KEYS)
        self.client.get("/logout")
        self._login("ui-admin")
        for mode in ("classic", "new"):
            with self.subTest(admin_mode=mode):
                self.service.set_user_credential(self.admin.id, "UI_MODE", mode)
                response = self.client.get(f"/admin/users/{self.reader.id}/integrations")
                self.assertEqual(response.status_code, 200)
                self.assertNotIn(f'name="{SERIES_KEY}"', response.get_data(as_text=True))

    def test_existing_series_value_survives_integrations_save_and_interface_roundtrip(self):
        self._real_templates()
        self._series_books()
        self.service.set_user_credential(self.reader.id, SERIES_KEY, "true")
        self._login("ui-reader")
        self.assertEqual(self.client.post(
            "/account/integrations", data={SERIES_KEY: "false"},
        ).status_code, 200)
        self.assertEqual(self.service.get_user_credential(self.reader.id, SERIES_KEY), "true")
        for mode in ("classic", "new"):
            with self.subTest(mode=mode):
                response = self.client.post(
                    "/account/interface?next=/account", data={"ui_mode": mode},
                    follow_redirects=True,
                )
                self.assertEqual((response.status_code, response.request.path), (200, "/account"))
                markup = response.get_data(as_text=True)
                self.assertIn('id="account-display"', markup)
                if mode == "classic":
                    self.assertNotIn('account-series-form', markup)
                else:
                    self.assertIn('account-series-form', markup)
                    self.assertRegex(
                        markup,
                        r'name="SERIES_SHOW_CURRENT_BOOK_DETAIL" value="true"\s+checked',
                    )
        self.assertEqual(self.service.get_user_credential(self.reader.id, SERIES_KEY), "true")
        self.assertIn('class="series-strip series-header"', self.client.get("/").get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
