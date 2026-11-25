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
        self.assertIn('href="/account/display"', classic)
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
        self.assertIn('name="UI_SHELF_FINISH"', markup)
        self.assertRegex(markup, r'id="bb-next-user-menu"[\s\S]*?href="/account/display"')
        self.assertRegex(markup, r'id="bb-next-mobile-user-menu"[\s\S]*?href="/account/display"')

    def test_hero_switch_appears_only_in_new_display_behaviour_group(self):
        self._real_templates()
        self.service.set_user_credential(self.reader.id, "UI_MODE", "new")
        self._login("ui-reader")
        markup = self.client.get("/account/display").get_data(as_text=True)
        behaviour = re.search(
            r'<section class="display-group" data-display-group="behaviour".*?</section>',
            markup, re.S)
        self.assertIsNotNone(behaviour)
        self.assertIn("Behaviour", behaviour.group())
        self.assertIn('data-display-setting="UI_SHOW_HERO"', behaviour.group())
        self.assertRegex(behaviour.group(), r'name="UI_SHOW_HERO" value="true" role="switch"[^>]*checked')
        response = self.client.post("/account/display?next=/account/display",
            data={"UI_SHOW_HERO": "off"}, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.service.get_user_credential(self.reader.id, "UI_SHOW_HERO"), "false")
        self.assertIn('name="UI_SHOW_HERO" value="true" role="switch" aria-checked="false"',
                      response.get_data(as_text=True))
        self.service.set_user_credential(self.reader.id, "UI_MODE", "classic")
        self.assertNotIn("Behaviour", self.client.get("/account/display").get_data(as_text=True))
        self.assertIsNone(self.service.get_user_credential(self.admin.id, "UI_SHOW_HERO"))

    def test_shelf_finish_rendering_visibility_and_fallback(self):
        self._real_templates()
        self._series_books()
        self.service.set_user_credential(self.reader.id, 'UI_MODE', 'new')
        self._login('ui-reader')
        for style in ('faceout', 'spines', 'cards', 'list'):
            self.service.set_user_credential(self.reader.id, 'UI_LIBRARY_STYLE', style)
            for finish in ('theme', 'wood', 'dark', 'plain', 'walnut', 'cherry', 'white', 'glass', 'pastel'):
                with self.subTest(style=style, finish=finish):
                    self.service.set_user_credential(self.reader.id, 'UI_SHELF_FINISH', finish)
                    html = self.client.get('/account/display').get_data(as_text=True)
                    match = re.search(r'<form class="account-shelf-finish-form.*?</form>', html, re.S)
                    self.assertIsNotNone(match)
                    form = match.group()
                    self.assertIn('action="/account/display?next=/account/display"', form)
                    self.assertIn('data-display-setting="UI_SHELF_FINISH"', form)
                    self.assertIn(f'<input type="hidden" name="UI_SHELF_FINISH" value="{finish}"', form)
                    self.assertRegex(form, rf'name="UI_SHELF_FINISH" value="{finish}"[^>]*aria-pressed="true"')
                    self.assertEqual(' hidden>' in form.split('>')[0] + '>', style in ('cards', 'list'))
                    self.assertIn(f'data-shelf-finish="{finish}"', html)
        self.service.set_user_credential(self.reader.id, 'UI_LIBRARY_STYLE', 'spines')
        response = self.client.post('/account/display?next=/account/display',
            data=MultiDict([('UI_SHELF_FINISH', 'theme'), ('UI_SHELF_FINISH', 'plain')]))
        self.assertEqual((response.status_code, response.location), (302, '/account/display'))
        self.assertEqual(self.service.get_user_credential(self.reader.id, 'UI_SHELF_FINISH'), 'plain')
        self.assertIsNone(self.service.get_user_credential(self.admin.id, 'UI_SHELF_FINISH'))

    def test_accent_and_library_background_render_save_and_stay_per_reader(self):
        self._real_templates()
        self.service.set_user_credential(self.reader.id, 'UI_MODE', 'new')
        self._login('ui-reader')
        for key, form_class, values, default in (
            ('UI_ACCENT', 'account-accent-form', ('amber', 'teal', 'rose', 'violet', 'blue'), 'amber'),
            ('UI_LIBRARY_BACKGROUND', 'account-background-form', ('none', 'cover'), 'none'),
            ('UI_COVER_FINISH', 'account-cover-finish-form', ('standard', 'hardcover', 'paperback', 'flat'), 'standard'),
        ):
            with self.subTest(key=key):
                html = self.client.get('/account/display').get_data(as_text=True)
                form = re.search(rf'<form class="{form_class}.*?</form>', html, re.S).group()
                self.assertIn(f'data-display-setting="{key}"', form)
                self.assertRegex(form, rf'name="{key}" value="{default}"[^>]*aria-pressed="true"')
                for value in values:
                    self.assertIn(f'name="{key}" value="{value}"', form)
                response = self.client.post('/account/display?next=/account/display', data={key: values[-1]})
                self.assertEqual((response.status_code, response.location), (302, '/account/display'))
                self.assertEqual(self.service.get_user_credential(self.reader.id, key), values[-1])
                self.assertIsNone(self.service.get_user_credential(self.admin.id, key))
                bogus = self.client.post('/account/display', json={key: 'neon'})
                self.assertEqual(bogus.status_code, 400)
        self.assertRegex(self.client.get('/account/display').get_data(as_text=True), r'<html[^>]*data-accent="blue"')
        self.service.set_user_credential(self.reader.id, 'UI_MODE', 'classic')
        classic = self.client.get('/account').get_data(as_text=True)
        self.assertNotIn('UI_ACCENT', classic)
        self.assertNotIn('UI_LIBRARY_BACKGROUND', classic)

    def test_shelf_props_switch_shows_only_for_shelf_styles_and_saves_per_reader(self):
        self._real_templates()
        self._series_books()
        self.service.set_user_credential(self.reader.id, 'UI_MODE', 'new')
        self._login('ui-reader')
        for style in ('faceout', 'spines', 'cards', 'list'):
            with self.subTest(style=style):
                self.service.set_user_credential(self.reader.id, 'UI_LIBRARY_STYLE', style)
                html = self.client.get('/account/display').get_data(as_text=True)
                form = re.search(r'<form class="account-shelf-props-form.*?</form>', html, re.S).group()
                self.assertEqual(' hidden>' in form.split('>')[0] + '>', style in ('cards', 'list'))
                self.assertIn('name="UI_SHELF_PROPS" value="true" role="switch" aria-checked="false"', form)
                finish = re.search(r'<form class="account-cover-finish-form.*?</form>', html, re.S).group()
                self.assertEqual(' hidden>' in finish.split('>')[0] + '>', style != 'faceout', 'cover finish is Face-out only')
        self.assertIn('/static/js/next/shelf-props.js', html)
        self.client.post('/account/display?next=/account/display', data={'UI_SHELF_PROPS': 'on'})
        self.assertEqual(self.service.get_user_credential(self.reader.id, 'UI_SHELF_PROPS'), 'true')
        self.assertIsNone(self.service.get_user_credential(self.admin.id, 'UI_SHELF_PROPS'))
        html = self.client.get('/account/display').get_data(as_text=True)
        self.assertIn('data-shelf-props="true"', html)
        self.assertIn('name="UI_SHELF_PROPS" value="true" role="switch" aria-checked="true"', html)

    def test_trinket_choices_and_amount_render_save_per_reader_and_reject_bad_values(self):
        self._real_templates()
        self._series_books()
        self.service.set_user_credential(self.reader.id, 'UI_MODE', 'new')
        self.service.set_user_credential(self.reader.id, 'UI_LIBRARY_STYLE', 'spines')
        self._login('ui-reader')

        def forms():
            html = self.client.get('/account/display').get_data(as_text=True)
            kinds = re.search(r'<form class="account-shelf-prop-kinds-form.*?</form>', html, re.S).group()
            amount = re.search(r'<form class="account-shelf-prop-amount-form.*?</form>', html, re.S).group()
            return html, kinds, amount

        html, kinds, amount = forms()
        self.assertIn('data-shelf-prop-kinds="bookends,plant,stack,candle" data-shelf-prop-amount="3"', html)
        for form in (kinds, amount):
            self.assertIn(' hidden>', form.split('>')[0] + '>', 'trinket rows wait for the Shelf props switch')
        for kind in ('bookends', 'plant', 'candle', 'stack'):
            self.assertIn(f'data-display-piece value="{kind}" checked', kinds)
        self.assertIn('name="UI_SHELF_PROP_AMOUNT" value="3"', amount)

        response = self.client.post('/account/display', json={
            'UI_SHELF_PROPS': 'true', 'UI_SHELF_PROP_KINDS': 'candle,bookends', 'UI_SHELF_PROP_AMOUNT': '5'}, headers={'Accept': 'application/json'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.service.get_user_credential(self.reader.id, 'UI_SHELF_PROP_KINDS'), 'bookends,candle')
        self.assertEqual(self.service.get_user_credential(self.reader.id, 'UI_SHELF_PROP_AMOUNT'), '5')
        for key in ('UI_SHELF_PROP_KINDS', 'UI_SHELF_PROP_AMOUNT'):
            self.assertIsNone(self.service.get_user_credential(self.admin.id, key))
        html, kinds, amount = forms()
        self.assertIn('data-shelf-prop-kinds="bookends,candle" data-shelf-prop-amount="5"', html)
        self.assertNotIn(' hidden>', kinds.split('>')[0] + '>')
        self.assertIn('data-display-piece value="candle" checked', kinds)
        self.assertIn('data-display-piece value="plant" >', kinds)
        self.assertIn('aria-label="More trinkets" disabled', amount)

        for bad in ({'UI_SHELF_PROP_KINDS': 'plant,dragon'}, {'UI_SHELF_PROP_AMOUNT': '0'}, {'UI_SHELF_PROP_AMOUNT': '6'}):
            with self.subTest(bad=bad):
                self.assertEqual(self.client.post('/account/display', json=bad).status_code, 400)
        self.assertEqual(self.service.get_user_credential(self.reader.id, 'UI_SHELF_PROP_KINDS'), 'bookends,candle')
        self.client.post('/account/display', json={'UI_SHELF_PROP_KINDS': ''})
        self.assertEqual(self.service.get_user_credential(self.reader.id, 'UI_SHELF_PROP_KINDS'), '')
        self.assertIn('data-shelf-prop-kinds=""', forms()[0], 'no trinkets is a real choice, not the default')

    def test_trinket_uploads_save_serve_owner_only_and_reach_the_shelves(self):
        import io
        import src.web_server as ws
        svg = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><rect width="10" height="10"/></svg>'
        self._real_templates()
        self._series_books()
        for key, value in (('UI_MODE', 'new'), ('UI_LIBRARY_STYLE', 'spines'), ('UI_SHELF_PROPS', 'true'),
                           ('UI_SHELF_PROP_KINDS', 'plant')):
            self.service.set_user_credential(self.reader.id, key, value)
        self._login('ui-reader')

        def upload(data, filename='mine.svg'):
            return self.client.post('/account/trinkets', data={'trinket': (io.BytesIO(data), filename)},
                                    content_type='multipart/form-data')

        response = upload(svg)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers['Location'].endswith('/account/display#display-trinkets'))
        folder = ws.DATA_DIR / 'uploads' / str(self.reader.id) / 'trinkets'
        [name] = [path.name for path in folder.iterdir()]
        self.assertRegex(name, r'^[0-9a-f]{12}\.svg$')
        kinds = self.service.get_user_credential(self.reader.id, 'UI_SHELF_PROP_KINDS')
        self.assertEqual(kinds, f'plant,upload:{name}', 'a new trinket goes straight onto the shelves')

        served = self.client.get(f'/account/trinkets/{name}')
        self.assertEqual(served.status_code, 200)
        self.assertEqual(served.data, svg)
        self.assertEqual(served.mimetype, 'image/svg+xml')
        self.assertIn('sandbox', served.headers['Content-Security-Policy'])
        self.assertIn("default-src 'none'", served.headers['Content-Security-Policy'])
        self.assertEqual(served.headers['X-Content-Type-Options'], 'nosniff')
        served.close()

        html = self.client.get('/account/display').get_data(as_text=True)
        self.assertIn(f'data-display-piece value="upload:{name}" checked', html)
        self.assertIn(f'formaction="/account/trinkets/{name}/delete"', html)
        self.assertIn(f'data-shelf-prop-kinds="plant,upload:{name}"', html)

        for data, filename, reason in ((b'<html>no</html>', 'x.svg', 'not-an-image'), (b'', 'x.png', 'empty'),
                                       (b'\x89PNG\r\n\x1a\n' + b'0' * (512 * 1024), 'big.png', 'too-big')):
            with self.subTest(reason=reason):
                response = upload(data, filename)
                self.assertIn(f'trinket_error={reason}', response.headers['Location'])
                page = self.client.get(response.headers['Location']).get_data(as_text=True)
                self.assertIn('class="display-upload-error" role="alert"', page)
        refused = self.client.post('/account/trinkets', data={'trinket': (io.BytesIO(b'nope'), 'x.svg')},
                                   content_type='multipart/form-data', headers={'Accept': 'application/json'})
        self.assertEqual((refused.status_code, refused.get_json()['error']), (400, 'not-an-image'))
        self.assertEqual(len(list(folder.iterdir())), 1, 'refused files are never written')

        # Someone else's picture: not served, and it cannot be chosen.
        self.client.post('/logout')
        self._login('ui-admin')
        self.assertEqual(self.client.get(f'/account/trinkets/{name}').status_code, 404)
        stolen = self.client.post('/account/display', json={'UI_SHELF_PROP_KINDS': f'plant,upload:{name}'})
        self.assertEqual(stolen.status_code, 400)
        self.assertIsNone(self.service.get_user_credential(self.admin.id, 'UI_SHELF_PROP_KINDS'))
        self.assertEqual(self.client.post(f'/account/trinkets/{name}/delete').status_code, 302)
        self.assertTrue((folder / name).exists(), "another reader's delete does nothing")

        self.client.post('/logout')
        self._login('ui-reader')
        self.client.post(f'/account/trinkets/{name}/delete')
        self.assertFalse((folder / name).exists())
        self.assertEqual(self.service.get_user_credential(self.reader.id, 'UI_SHELF_PROP_KINDS'), 'plant')
        self.assertEqual(self.client.get(f'/account/trinkets/{name}').status_code, 404)

    def test_classic_menu_display_link_opens_existing_card(self):
        self._real_templates()
        self.service.set_user_credential(self.reader.id, 'UI_MODE', 'classic')
        self._login('ui-reader')
        html = self.client.get('/account').get_data(as_text=True)
        menu = re.search(r'<div class="user-menu-dropdown" role="menu">(.*?)</div>', html, re.S).group(1)
        self.assertRegex(menu, r'href="/account"[^>]*role="menuitem"')
        self.assertRegex(menu, r'href="/account/display"[^>]*role="menuitem"')
        self.assertLess(menu.index('href="/account"'), menu.index('href="/account/display"'))
        self.assertLess(menu.index('href="/account/display"'), menu.index('Docs'))
        self.assertIn('id="account-display"', html)
        self.assertIn('name="ui_mode" value="new"', html)
        self.assertIn('class="account-series-form"', html)
        redirect = self.client.get('/account/display')
        self.assertEqual((redirect.status_code, redirect.location), (302, '/account#account-display'))

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
        with patch('src.web.dashboard_projection._prefetch_bookorbit_authors', return_value={}) as authors,                 patch('src.web.dashboard_projection._prefetch_bookfusion_links', return_value={}) as links:
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


    def test_cover_controls_render_saved_values_and_fallback_posts(self):
        self._real_templates()
        self.service.set_user_credential(self.reader.id, "UI_MODE", "new")
        self._login("ui-reader")
        switches = ("UI_SHOW_PROGRESS", "UI_SHOW_FINISHED", "UI_SHOW_FORMAT_STRIP",
                    "UI_SHOW_RATINGS", "UI_SHOW_TITLES")
        for size in ("1", "2", "5"):
            with self.subTest(size=size):
                self.service.set_user_credential(self.reader.id, "UI_COVER_SIZE", size)
                markup = self.client.get("/account/display").get_data(as_text=True)
                size_form = re.search(r'<form class="account-cover-size-form.*?</form>', markup, re.S).group()
                self.assertIn('action="/account/display?next=/account/display"', size_form)
                self.assertIn(f'name="UI_COVER_SIZE" value="{size}"', size_form)
                down = re.search(r'<button[^>]*data-display-step="-1"[^>]*>', size_form).group()
                up = re.search(r'<button[^>]*data-display-step="1"[^>]*>', size_form).group()
                self.assertEqual("disabled" in down, size == "1")
                self.assertEqual("disabled" in up, size == "5")
                self.assertEqual(len(re.findall(r'data-size-dot=', size_form)), 5)
        for key in switches:
            with self.subTest(key=key):
                self.service.set_user_credential(self.reader.id, key, "false")
                markup = self.client.get("/account/display").get_data(as_text=True)
                form = re.search(r'<form class="account-cover-toggle-form[^"]*"[^>]*data-display-setting="'+key+r'".*?</form>', markup, re.S).group()
                self.assertIn('action="/account/display?next=/account/display"', form)
                self.assertRegex(form, rf'name="{key}" value="false"')
                self.assertRegex(form, rf'name="{key}" value="true" role="switch" aria-checked="false"')
                self.assertEqual(self.client.post("/account/display?next=/account/display",
                    data=MultiDict([(key, "false"), (key, "true")])).status_code, 302)
                self.assertEqual(self.service.get_user_credential(self.reader.id, key), "true")
        self.assertIsNone(self.service.get_user_credential(self.admin.id, "UI_SHOW_TITLES"))
        self.assertEqual(self.client.post("/account/display", data={"UI_COVER_SIZE": "3"}).status_code, 302)
        self.assertEqual(self.service.get_user_credential(self.reader.id, "UI_COVER_SIZE"), "3")
        self.service.set_user_credential(self.reader.id, "UI_LIBRARY_STYLE", "spines")
        markup = self.client.get("/account/display").get_data(as_text=True)
        self.assertRegex(markup, r'<form class="account-cover-size-form[^"]*"[^>]*hidden>')
        self.service.set_user_credential(self.reader.id, "UI_LIBRARY_STYLE", "list")
        self.assertRegex(self.client.get("/account/display").get_data(as_text=True),
                         r'<form class="account-cover-size-form[^"]*"[^>]*hidden>')

    def test_preview_selects_up_to_eight_reader_books_and_no_private_books(self):
        self._real_templates()
        self.service.set_user_credential(self.reader.id, "UI_MODE", "new")
        for index in range(10):
            book_id = f"preview-reader-{index:02d}"
            self.service.save_book(Book(abs_id=book_id, abs_title=f"Preview reader {index:02d}",
                                        status="active", ebook_filename=f"{book_id}.epub", duration=1000))
            self.service.link_user_book(self.reader.id, book_id)
        self.service.save_book(Book(abs_id="preview-private", abs_title="Preview private",
                                    status="active", duration=1000))
        self.service.link_user_book(self.admin.id, "preview-private")
        self._login("ui-reader")
        markup = self.client.get("/account/display").get_data(as_text=True)
        for index in range(8):
            self.assertIn(f"Preview reader {index:02d}", markup)
        for index in (8, 9):
            self.assertNotIn(f"Preview reader {index:02d}", markup)
        self.assertNotIn("Preview private", markup)
        self.assertEqual(len(set(re.findall(r'data-abs-id="(preview-reader-[0-9]+)"', markup))), 8)


if __name__ == "__main__":
    unittest.main()


class TestDisplayPageScript(unittest.TestCase):
    """Runs the Display page's node checks (save, rollback, preview, theme bridge) in the suite."""

    def test_display_node_regressions(self):
        import shutil
        import subprocess
        from pathlib import Path
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js not available")
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [node, str(root / "tests/test_ui_new_display.js")],
            cwd=root, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_shelf_props_node_regressions(self):
        import shutil
        import subprocess
        from pathlib import Path
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js not available")
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [node, str(root / "tests/test_ui_shelf_props.js")],
            cwd=root, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
