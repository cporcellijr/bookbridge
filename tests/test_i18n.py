"""i18n: locale resolution and the per-user language preference.

These tests exercise the real app (real DatabaseService, auth enabled) so the
locale selector runs against a real logged-in user. A tiny ru catalog is built
in a temp dir for the render test — the repo ships no catalogs yet, and the
language picker must not offer a language we cannot render.
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from babel.messages.catalog import Catalog
from babel.messages.mofile import write_mo

from src.db.database_service import DatabaseService
from tests.test_webserver import MockContainer

_TEMPLATES = str(Path(__file__).parent.parent / "templates")


def _write_catalog(directory: str, locale: str, messages: dict) -> None:
    """Build translations/<locale>/LC_MESSAGES/messages.mo from a plain dict."""
    lc_dir = Path(directory) / locale / "LC_MESSAGES"
    lc_dir.mkdir(parents=True, exist_ok=True)
    catalog = Catalog(locale=locale, domain="messages")
    for msgid, msgstr in messages.items():
        catalog.add(msgid, string=msgstr)
    with open(lc_dir / "messages.mo", "wb") as fh:
        write_mo(fh, catalog)
    # Catalog discovery is cached; drop it so the new catalog is seen.
    from src.web_server import available_locales
    available_locales.cache_clear()


class I18nTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ['DATA_DIR'] = self.tmp
        os.environ['BOOKS_DIR'] = self.tmp
        self._orig_template_dir = os.environ.get('TEMPLATE_DIR')
        os.environ['TEMPLATE_DIR'] = _TEMPLATES
        # No catalog directory unless a test opts in.
        self.translations = os.path.join(self.tmp, "translations")
        self._orig_translations_dir = os.environ.get('TRANSLATIONS_DIR')
        os.environ['TRANSLATIONS_DIR'] = self.translations

        from src.web_server import available_locales
        available_locales.cache_clear()

        self.svc = DatabaseService(os.path.join(self.tmp, "i18n.db"))
        self.admin = self.svc.create_user("admin", "secret", role="admin")

        self.mock_container = MockContainer()
        self.mock_container.mock_database_service = self.svc

        import src.db.migration_utils
        self._orig_init = src.db.migration_utils.initialize_database
        src.db.migration_utils.initialize_database = lambda data_dir: self.svc

        from src.web_server import create_app, get_locale
        self.app, _ = create_app(test_container=self.mock_container)
        self.app.config['TESTING'] = True
        self.app.config['LOGIN_DISABLED'] = False

        # Probe route reporting the locale the selector resolves for this
        # request. Registered before the first request (Flask forbids later).
        self.app.add_url_rule('/_i18n_probe', '_i18n_probe',
                              lambda: str(get_locale()))

        self.client = self.app.test_client()

    def tearDown(self):
        import src.db.migration_utils
        src.db.migration_utils.initialize_database = self._orig_init
        for key, value in (('TEMPLATE_DIR', self._orig_template_dir),
                           ('TRANSLATIONS_DIR', self._orig_translations_dir)):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.tmp, ignore_errors=True)
        # After the env is restored, drop the cache built from this test's
        # (now-deleted) temp catalog so later modules see the real dir.
        from src.web_server import available_locales
        available_locales.cache_clear()

    def _login(self, username="admin", password="secret"):
        resp = self.client.post('/login', data={'username': username, 'password': password},
                                follow_redirects=False)
        self.assertEqual(resp.status_code, 302)

    def _locale_of(self, path='/account', headers=None):
        """Resolve get_locale() as the running app would for a real request.

        Hits the probe route registered in setUp, so it runs inside the client's
        logged-in session — the selector reads users.language from current_user(),
        which needs the session.
        """
        resp = self.client.get('/_i18n_probe', headers=headers or {})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        return resp.get_data(as_text=True)


class AvailableLocalesTests(I18nTestBase):
    """The offered languages come from real catalogs, not a hardcoded list."""

    def test_only_english_when_no_catalogs(self):
        from src.web_server import available_locales
        self.assertEqual(available_locales(), ('en',))

    def test_language_appears_once_its_catalog_exists(self):
        from src.web_server import available_locales
        _write_catalog(self.translations, 'ru', {'Library': 'Библиотека'})
        self.assertEqual(available_locales(), ('en', 'ru'))

    def test_display_name_is_the_language_in_itself(self):
        from src.web_server import locale_display_name
        self.assertEqual(locale_display_name('ru'), 'Русский')
        self.assertEqual(locale_display_name('en'), 'English')
        # Unknown code degrades to the code itself, never raises.
        self.assertEqual(locale_display_name('zz'), 'zz')

    def test_picker_is_hidden_when_only_english(self):
        self._login()
        body = self.client.get('/account').get_data(as_text=True)
        self.assertNotIn('name="language"', body)

    def test_picker_shown_when_a_second_language_exists(self):
        _write_catalog(self.translations, 'ru', {'Library': 'Библиотека'})
        self._login()
        body = self.client.get('/account').get_data(as_text=True)
        self.assertIn('name="language"', body)


class LocaleResolutionTests(I18nTestBase):
    """Resolution order: users.language -> Accept-Language -> English."""

    def test_falls_back_to_english(self):
        self._login()
        self.assertEqual(self._locale_of(), 'en')

    def test_accept_language_used_when_no_catalog_for_it(self):
        # 'ru' is not available (no catalog) -> best_match falls to English.
        self._login()
        self.assertEqual(self._locale_of(headers={'Accept-Language': 'ru-RU,ru;q=0.9'}), 'en')

    def test_accept_language_honoured_when_available(self):
        _write_catalog(self.translations, 'ru', {'Library': 'Библиотека'})
        self._login()
        self.assertEqual(self._locale_of(headers={'Accept-Language': 'ru-RU,ru;q=0.9'}), 'ru')

    def test_user_language_column_wins(self):
        _write_catalog(self.translations, 'ru', {'Library': 'Библиотека'})
        self.svc.set_user_language(self.admin.id, 'ru')
        self._login()
        self.assertEqual(self._locale_of(headers={'Accept-Language': 'en-US,en;q=0.9'}), 'ru')

    def test_stored_language_ignored_when_no_catalog(self):
        # Chosen 'ru' but the catalog is gone: must not crash, must fall back.
        self.svc.set_user_language(self.admin.id, 'ru')
        self._login()
        self.assertEqual(self._locale_of(), 'en')

    def test_locale_normalised_from_hyphenated_tag(self):
        from src.web_server import _normalize_locale
        self.assertEqual(_normalize_locale('ru-RU'), 'ru')
        self.assertEqual(_normalize_locale('ru_RU'), 'ru')
        self.assertEqual(_normalize_locale('RU'), 'ru')
        self.assertIsNone(_normalize_locale(None))
        self.assertIsNone(_normalize_locale(''))


class LanguageSettingTests(I18nTestBase):
    def test_saving_language_updates_the_column(self):
        _write_catalog(self.translations, 'ru', {'Library': 'Библиотека'})
        self._login()
        resp = self.client.post('/account/language', data={'language': 'ru'},
                                follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.svc.get_user(self.admin.id).language, 'ru')

    def test_unknown_language_is_rejected(self):
        self._login()
        resp = self.client.post('/account/language', data={'language': 'zz'}, follow_redirects=False)
        self.assertEqual(resp.status_code, 400)
        self.assertIsNone(self.svc.get_user(self.admin.id).language)

    def test_unknown_language_keeps_the_saved_choice(self):
        _write_catalog(self.translations, 'ru', {'Library': 'Библиотека'})
        self.svc.set_user_language(self.admin.id, 'ru')
        self._login()
        resp = self.client.post('/account/language', data={'language': 'zz'}, follow_redirects=False)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.svc.get_user(self.admin.id).language, 'ru')

    def test_missing_language_keeps_the_saved_choice(self):
        _write_catalog(self.translations, 'ru', {'Library': 'Библиотека'})
        self.svc.set_user_language(self.admin.id, 'ru')
        self._login()
        resp = self.client.post('/account/language', data={}, follow_redirects=False)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.svc.get_user(self.admin.id).language, 'ru')

    def test_clearing_language_is_allowed(self):
        _write_catalog(self.translations, 'ru', {'Library': 'Библиотека'})
        self.svc.set_user_language(self.admin.id, 'ru')
        self._login()
        self.client.post('/account/language', data={'language': ''}, follow_redirects=False)
        self.assertIsNone(self.svc.get_user(self.admin.id).language)


class RenderedTranslationTests(I18nTestBase):
    """A wrapped template really renders the catalog's string."""

    def test_nav_renders_russian(self):
        _write_catalog(self.translations, 'ru', {'Library': 'Библиотека'})
        self._login()
        self.client.post('/account/language', data={'language': 'ru'}, follow_redirects=False)
        body = self.client.get('/').get_data(as_text=True)
        self.assertIn('Библиотека', body)

    def test_nav_renders_english_without_catalog(self):
        self._login()
        body = self.client.get('/').get_data(as_text=True)
        self.assertIn('Library', body)

    def test_account_page_renders_russian(self):
        _write_catalog(self.translations, 'ru', {
            'Change password': 'Сменить пароль',
            'Signed in as <strong>%(name)s</strong>': 'Вы вошли как <strong>%(name)s</strong>',
        })
        self._login()
        self.client.post('/account/language', data={'language': 'ru'}, follow_redirects=False)
        body = self.client.get('/account').get_data(as_text=True)
        self.assertIn('Сменить пароль', body)
        self.assertIn('Вы вошли как <strong>admin</strong>', body)

    def test_add_book_renders_russian(self):
        _write_catalog(self.translations, 'ru', {
            'Add / Update Book': 'Добавить / обновить книгу',
            'Reader Documents': 'Документы читалки',
            'Searching...': 'Поиск...',
        })
        self._login()
        self.client.post('/account/language', data={'language': 'ru'}, follow_redirects=False)
        body = self.client.get('/add-book').get_data(as_text=True)
        self.assertIn('<h2>Добавить / обновить книгу</h2>', body)
        self.assertIn('Документы читалки', body)
        # data-working-label is shown on the button while a search runs.
        self.assertIn('data-working-label="Поиск..."', body)

    def test_integrations_pages_render_russian(self):
        _write_catalog(self.translations, 'ru', {
            'Save integrations': 'Сохранить интеграции',
            'Integrations - %(name)s': 'Интеграции: %(name)s',
            'Regular users need explicit account credentials here. Blank account fields stay '
            'unavailable instead of inheriting the admin/master library.':
                'Обычным пользователям нужны собственные учётные данные.',
        })
        user = self.svc.create_user("reader", "secret")
        self._login()
        self.client.post('/account/language', data={'language': 'ru'}, follow_redirects=False)
        body = self.client.get('/account/integrations').get_data(as_text=True)
        self.assertIn('Сохранить интеграции', body)
        body = self.client.get(f'/admin/users/{user.id}/integrations').get_data(as_text=True)
        self.assertIn('Интеграции: reader', body)
        self.assertIn('Обычным пользователям нужны собственные учётные данные.', body)

    def test_logs_stats_suggestions_render_russian(self):
        _write_catalog(self.translations, 'ru', {
            'Application Logs': 'Журнал приложения',
            'Unified activity dashboard': 'Сводная панель активности',
            'Library Suggestions': 'Предложения по библиотеке',
            # Counts go through ngettext, so Russian gets all three plural forms.
            ('%(num)s item', '%(num)s items'):
                ('%(num)s элемент', '%(num)s элемента', '%(num)s элементов'),
        })
        self._login()
        self.client.post('/account/language', data={'language': 'ru'}, follow_redirects=False)
        self.assertIn('Журнал приложения', self.client.get('/logs').get_data(as_text=True))
        self.assertIn('Сводная панель активности', self.client.get('/stats').get_data(as_text=True))
        body = self.client.get('/suggestions').get_data(as_text=True)
        self.assertIn('Предложения по библиотеке', body)
        self.assertIn('id="queueCount">0 элементов</span>', body)

    def test_confirm_survives_an_apostrophe_in_the_translation(self):
        # A translated confirm() string is a JS string literal: an apostrophe in
        # the msgstr (French "l'ensemble") must not end it early. tojson escapes
        # it to \u0027, so the dialog keeps working whatever the translation.
        self.svc.create_user("o'brien", "secret")
        self.svc.create_user("<i>x", "secret")
        _write_catalog(self.translations, 'ru', {
            'Delete %(name)s? Their progress/credentials are removed.':
                "Supprimer l'utilisateur %(name)s ?",
        })
        self._login()
        self.client.post('/account/language', data={'language': 'ru'}, follow_redirects=False)
        body = self.client.get('/admin/users').get_data(as_text=True)
        self.assertIn('confirm("Supprimer l\\u0027utilisateur o\\u0027brien ?")', body)
        self.assertNotIn("Supprimer l'", body)
        # The name is not HTML-escaped by _() (that would show "&#39;" in the
        # dialog) and is still safe: tojson escapes markup characters.
        self.assertIn('confirm("Supprimer l\\u0027utilisateur \\u003ci\\u003ex ?")', body)
        self.assertNotIn('<i>x', body)


class TemplateMessageTests(unittest.TestCase):
    """Every msgid extracted from the templates must survive newstyle gettext.

    Flask-Babel enables Jinja's newstyle gettext, which always applies
    ``msgid % variables`` -- even with no variables. A bare ``%`` in a ``_()``
    string (``_('50% done')``) therefore raises at render time, and only on the
    page that contains it. ``{% trans %}`` blocks escape ``%`` themselves; ``_()``
    strings must write ``%%``. This catches it at test time for every template.
    """

    def test_msgids_are_valid_newstyle_format_strings(self):
        from collections import defaultdict
        from babel.messages.extract import extract_from_dir

        method_map = [('templates/**.html', 'jinja2')]
        options_map = {'templates/**.html': {'extensions': 'jinja2.ext.i18n'}}
        root = str(Path(__file__).parent.parent)
        bad = []
        for filename, lineno, message, _comments, _ctx in extract_from_dir(
                root, method_map, options_map):
            for msgid in (message if isinstance(message, tuple) else (message,)):
                try:
                    msgid % defaultdict(str)
                except (ValueError, TypeError, KeyError) as exc:
                    bad.append(f"{filename}:{lineno}: {msgid!r} ({exc})")
        self.assertEqual(bad, [], "msgids that break newstyle gettext:\n" + "\n".join(bad))


if __name__ == '__main__':
    unittest.main()
