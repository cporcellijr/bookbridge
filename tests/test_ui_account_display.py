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

    def test_account_display_shows_interface_and_series_in_both_modes(self):
        self._real_templates()
        self._login('ui-reader')
        self.service.set_user_credential(self.reader.id, 'UI_MODE', 'classic')
        classic = self.client.get('/account').get_data(as_text=True)
        self.assertIn('id="account-display"', classic)
        self.assertIn('class="account-series-form"', classic)
        self.assertNotIn('href="/account/display"', classic)
        redirect = self.client.get('/account/display')
        self.assertEqual((redirect.status_code, redirect.location), (302, '/account#account-display'))
        self.service.set_user_credential(self.reader.id, 'UI_MODE', 'new')
        account = self.client.get('/account').get_data(as_text=True)
        self.assertNotIn('id="account-display"', account)
        self.assertNotIn('account-series-form', account)
        self.assertIn('href="/account/display"', account)
        page = self.client.get('/account/display')
        self.assertEqual(page.status_code, 200)
        markup = page.get_data(as_text=True)
        for name in ('account-interface-form', 'account-theme-form', 'account-library-style-form', 'account-series-form'):
            self.assertIn(f'class="{name}', markup)
        self.assertIn('action="/account/interface?next=/account/display"', markup)
        self.assertIn('action="/account/display?next=/account/display"', markup)
        self.assertIn('role="switch"', markup)
        for value in ('cards', 'faceout', 'spines', 'list'):
            self.assertIn(f'name="UI_LIBRARY_STYLE" value="{value}"', markup)
        for future in ('UI_COVER_SIZE', 'UI_SHOW_PROGRESS', 'UI_SHOW_FINISHED', 'UI_SHOW_FORMAT_STRIP', 'UI_SHOW_RATINGS', 'UI_SHOW_TITLES', 'UI_SHELF_FINISH'):
            self.assertNotIn(f'name="{future}"', markup)
        self.assertRegex(markup, r'id="bb-next-user-menu"[\s\S]*?href="/account/display"')
        self.assertRegex(markup, r'id="bb-next-mobile-user-menu"[\s\S]*?href="/account/display"')

    def test_display_preview_is_only_the_signed_in_readers_library(self):
        self._real_templates()
        from src.db.models import Book
        self.service.set_user_credential(self.reader.id, 'UI_MODE', 'new')
        self.service.set_user_credential(self.admin.id, 'UI_MODE', 'new')
        self._series_books()
        self.service.save_book(Book(abs_id='other-user-book', abs_title='Private admin book', status='active', duration=1000))
        self.service.link_user_book(self.admin.id, 'other-user-book')
        self._login('ui-reader')
        markup = self.client.get('/account/display').get_data(as_text=True)
        self.assertIn('Series book 1', markup)
        self.assertIn('Series book 2', markup)
        self.assertNotIn('Private admin book', markup)
        self.assertIn('display-preview-series-on', markup)
        self.assertIn('display-preview-series-off', markup)
        self.client.get('/logout')
        self._login('ui-admin')
        markup = self.client.get('/account/display').get_data(as_text=True)
        self.assertIn('Private admin book', markup)
        # Admin's shared catalog links may include both books; the reader must never see admin-only titles.

    def test_display_preview_resolves_authors_like_the_library(self):
        from unittest.mock import patch
        self._real_templates()
        self.service.set_user_credential(self.reader.id, 'UI_MODE', 'new')
        self._series_books()
        self._login('ui-reader')
        with patch('src.web_server._prefetch_bookorbit_authors', return_value={}) as authors,                 patch('src.web_server._prefetch_bookfusion_links', return_value={}) as links:
            self.assertEqual(self.client.get('/account/display').status_code, 200)
        authors.assert_called_once()
        links.assert_called_once()

    def test_display_get_signed_out_redirects_and_empty_reader_shows_note(self):
        self._real_templates()
        response = self.client.get('/account/display')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login', response.location)
        self.service.set_user_credential(self.reader.id, 'UI_MODE', 'new')
        self._login('ui-reader')
        markup = self.client.get('/account/display').get_data(as_text=True)
        self.assertIn('Your library is empty', markup)
        self.assertNotIn('id="display-preview-stage"', markup)

    def test_library_style_form_saves_only_the_reader_and_reflects_the_choice(self):
        self._real_templates()
        self.service.set_user_credential(self.reader.id, 'UI_MODE', 'new')
        self._series_books()
        self._login('ui-reader')
        for preference in ('cards', 'faceout', 'spines', 'list'):
            with self.subTest(preference=preference):
                response = self.client.post('/account/display?next=/account/display', data={'UI_LIBRARY_STYLE': preference}, follow_redirects=True)
                self.assertEqual((response.status_code, response.request.path), (200, '/account/display'))
                markup = response.get_data(as_text=True)
                form = re.search(r'<form class="account-library-style-form[^"]*"[^>]*>.*?</form>', markup, re.S)
                self.assertIsNotNone(form)
                self.assertRegex(form.group(), rf'value="{preference}"[^>]*aria-pressed="true"')
                self.assertIn(f'data-library-style="{preference}"', markup)
                self.assertEqual(self.service.get_user_credential(self.reader.id, 'UI_LIBRARY_STYLE'), preference)
                self.assertIsNone(self.service.get_user_credential(self.admin.id, 'UI_LIBRARY_STYLE'))
        self.client.get('/logout')
        self._login('ui-admin')
        self.service.set_user_credential(self.admin.id, 'UI_MODE', 'new')
        other = self.client.get('/account/display').get_data(as_text=True)
        self.assertRegex(other, r'value="cards"[^>]*aria-pressed="true"')

    def test_preview_hide_rules_beat_faceout_grid_specificity(self):
        from pathlib import Path
        css = (Path(__file__).resolve().parents[1] / 'static/css/next.css').read_text(encoding='utf-8')
        for view, hidden in (('spines', 'grouped'), ('list', 'grouped'), ('covers', 'flat')):
            with self.subTest(view=view):
                selector = f'.display-preview .library-page[data-view="{view}"] > .book-grid.display-preview-{hidden}'
                self.assertIn(selector, css)
        self.assertIn('display-preview-grouped,', css)

    def test_no_javascript_buttons_override_hidden_fallback(self):
        self._real_templates()
        self.service.set_user_credential(self.reader.id, 'UI_MODE', 'new')
        self._login('ui-reader')
        markup = self.client.get('/account/display').get_data(as_text=True)
        for form_class, field, choice in (
            ('account-theme-form', 'UI_THEME', 'night'),
            ('account-library-style-form', 'UI_LIBRARY_STYLE', 'faceout'),
        ):
            with self.subTest(field=field):
                form = re.search(r'<form class="'+form_class+r'[^"]*"[^>]*>.*?</form>', markup, re.S).group()
                button = f'name="{field}" value="{choice}" data-display-value="{choice}"'
                self.assertIn(button, form)
                self.assertIn(f'<input type="hidden" name="{field}" value="', form)
                self.assertLess(form.index(f'<input type="hidden" name="{field}"'), form.index(button))
                response = self.client.post('/account/display?next=/account/display',
                    data=MultiDict([(field, 'cards' if field == 'UI_LIBRARY_STYLE' else 'auto'), (field, choice)]))
                self.assertEqual((response.status_code, response.location), (302, '/account/display'))
                self.assertEqual(self.service.get_user_credential(self.reader.id, field), choice)

    def test_theme_form_saves_for_one_reader_and_reflects_the_choice(self):
        self._real_templates()
        self.service.set_user_credential(self.reader.id, 'UI_MODE', 'new')
        self._login('ui-reader')
        for preference in ('auto', 'day', 'night'):
            with self.subTest(preference=preference):
                response = self.client.post('/account/display?next=/account/display', data={'UI_THEME': preference}, follow_redirects=True)
                self.assertEqual((response.status_code, response.request.path), (200, '/account/display'))
                markup = response.get_data(as_text=True)
                form = re.search(r'<form class="account-theme-form[^"]*"[^>]*>.*?</form>', markup, re.S)
                self.assertIsNotNone(form)
                self.assertRegex(form.group(), rf'value="{preference}"[^>]*aria-pressed="true"')
                self.assertIn(f'data-theme-pref="{preference}"', markup)
                self.assertEqual(self.service.get_user_credential(self.reader.id, 'UI_THEME'), preference)
                self.assertIsNone(self.service.get_user_credential(self.admin.id, 'UI_THEME'))
        self.client.get('/logout')
        self._login('ui-admin')
        self.service.set_user_credential(self.admin.id, 'UI_MODE', 'new')
        other = self.client.get('/account/display').get_data(as_text=True)
        self.assertRegex(other, r'value="auto"[^>]*aria-pressed="true"')

    def test_series_form_saves_only_reader_and_changes_real_library_markup(self):
        self._real_templates()
        self._series_books()
        self.service.set_user_credential(self.reader.id, 'UI_MODE', 'new')
        self._login('ui-reader')
        display = self.client.get('/account/display').get_data(as_text=True)
        self.assertIn('class="account-series-form display-row"', display)
        self.assertIn('name="SERIES_SHOW_CURRENT_BOOK_DETAIL" value="false"', display)
        self.assertIn('data-series-on="false"', display)
        self.assertIn('display-preview-series-on', display)
        before = self.client.get('/').get_data(as_text=True)
        self.assertIn('data-series-name="P15b Series"', before)
        self.assertNotIn('class="series-strip series-header"', before)
        enabled = self.client.post('/account/display?next=/account/display', data=MultiDict([
            (SERIES_KEY, 'false'), (SERIES_KEY, 'true')]))
        self.assertEqual((enabled.status_code, enabled.location), (302, '/account/display'))
        self.assertEqual(self.service.get_user_credential(self.reader.id, SERIES_KEY), 'true')
        self.assertIsNone(self.service.get_user_credential(self.admin.id, SERIES_KEY))
        display = self.client.get('/account/display').get_data(as_text=True)
        self.assertRegex(display, r'name="SERIES_SHOW_CURRENT_BOOK_DETAIL" value="true" role="switch" aria-checked="true"\s+checked')
        self.assertIn('data-series-on="true"', display)
        self.assertIn('class="series-strip series-header"', self.client.get('/').get_data(as_text=True))
        disabled = self.client.post('/account/display?next=/account/display', data={SERIES_KEY: 'false'})
        self.assertEqual((disabled.status_code, disabled.location), (302, '/account/display'))
        self.assertNotIn('class="series-strip series-header"', self.client.get('/').get_data(as_text=True))

    def test_classic_series_form_saves_per_reader_and_updates_classic_library(self):
        self._real_templates()
        self._series_books()
        self.service.set_user_credential(self.reader.id, "UI_MODE", "classic")
        self._login("ui-reader")

        before = self.client.get("/account").get_data(as_text=True)
        form = re.search(r'<form class="account-series-form"[^>]*>.*?</form>', before, re.S)
        self.assertIsNotNone(form)
        self.assertIn('action="/account/display?next=/account"', form.group())
        self.assertIn('name="SERIES_SHOW_CURRENT_BOOK_DETAIL" value="false"', form.group())
        self.assertNotIn('value="true" checked', form.group())
        self.assertNotIn('<div class="series-header series-strip"', self.client.get("/").get_data(as_text=True))

        enabled = self.client.post(
            "/account/display?next=/account",
            data=MultiDict([(SERIES_KEY, "false"), (SERIES_KEY, "true")]),
        )
        self.assertEqual((enabled.status_code, enabled.location), (302, "/account"))
        self.assertEqual(self.service.get_user_credential(self.reader.id, SERIES_KEY), "true")
        self.assertIsNone(self.service.get_user_credential(self.admin.id, SERIES_KEY))
        self.assertRegex(self.client.get("/account").get_data(as_text=True),
                         r'name="SERIES_SHOW_CURRENT_BOOK_DETAIL" value="true"\s+checked')
        self.assertIn('<div class="series-header series-strip"', self.client.get("/").get_data(as_text=True))

        disabled = self.client.post("/account/display?next=/account", data={SERIES_KEY: "false"})
        self.assertEqual((disabled.status_code, disabled.location), (302, "/account"))
        self.assertEqual(self.service.get_user_credential(self.reader.id, SERIES_KEY), "false")
        self.assertIsNone(self.service.get_user_credential(self.admin.id, SERIES_KEY))
        self.assertNotIn('<div class="series-header series-strip"', self.client.get("/").get_data(as_text=True))

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
        self.service.set_user_credential(self.reader.id, SERIES_KEY, 'true')
        self._login('ui-reader')
        self.assertEqual(self.client.post('/account/integrations', data={SERIES_KEY: 'false'}).status_code, 200)
        self.assertEqual(self.service.get_user_credential(self.reader.id, SERIES_KEY), 'true')
        for mode in ('classic', 'new'):
            with self.subTest(mode=mode):
                response = self.client.post('/account/interface?next=/account', data={'ui_mode': mode}, follow_redirects=True)
                self.assertEqual((response.status_code, response.request.path), (200, '/account'))
                account = response.get_data(as_text=True)
                if mode == 'classic':
                    self.assertIn('id="account-display"', account)
                    self.assertIn('account-series-form', account)
                    self.assertRegex(account, r'name="SERIES_SHOW_CURRENT_BOOK_DETAIL" value="true"\s+checked')
                else:
                    self.assertNotIn('account-series-form', account)
                    display = self.client.get('/account/display').get_data(as_text=True)
                    self.assertIn('class="account-series-form display-row"', display)
                    self.assertIn('data-series-on="true"', display)
        self.assertEqual(self.service.get_user_credential(self.reader.id, SERIES_KEY), 'true')
        self.assertIn('class="series-strip series-header"', self.client.get('/').get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
