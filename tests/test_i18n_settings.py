"""i18n: the Settings page (settings.html markup).

Kept in its own module so this step merges independently of the other i18n
PRs that append to tests/test_i18n.py. Uses the same real-app base class and
a tiny Russian catalog built in a temp dir.
"""

import re
import unittest
from pathlib import Path

from babel.messages.extract import extract_from_file

from tests.test_i18n import I18nTestBase, _write_catalog

_SETTINGS = Path(__file__).parent.parent / "templates" / "settings.html"

_KOSYNC_NOTE = (
    "🔒 <strong>Per-user login.</strong> Each user's KoSync username &amp; password are set per "
    "user in <a href=\"%(settings_url)s#users\" style=\"color:#aab4f5;\">Settings → Users → "
    "(user) → Integrations</a> (including your own)."
)
_SESSION_GAP = (
    "The gap actually used is <strong><span id=\"effective_session_gap\">%(gap)s</span> "
    "minutes</strong>: at least twice the sync interval, at most 30. A session also ends when "
    "you finish the book or after four hours."
)

_RU = {
    "🔄 Reading &amp; Sync": "🔄 Чтение и синхронизация",
    "Different from defaults": "Отличается от значений по умолчанию",
    "💾 Save Settings": "💾 Сохранить настройки",
    "Find a setting…": "Найти настройку…",
    _KOSYNC_NOTE: (
        "🔒 <strong>Вход для каждого пользователя.</strong> Имя и пароль KoSync задаются для "
        "каждого пользователя в <a href=\"%(settings_url)s#users\" style=\"color:#aab4f5;\">"
        "Настройки → Пользователи → (пользователь) → Интеграции</a> (в том числе для вас)."
    ),
    _SESSION_GAP: (
        "Фактически используется промежуток <strong><span id=\"effective_session_gap\">%(gap)s"
        "</span> мин.</strong>: не меньше двух интервалов синхронизации и не больше 30. Сеанс "
        "также заканчивается, когда вы дочитываете книгу, или через четыре часа."
    ),
    "admin": "администратор",
    "active": "активен",
    "Integrations": "Интеграции",
    "new password": "новый пароль",
}


class SettingsPageTests(I18nTestBase):
    def _settings(self, locale=None):
        if locale:
            _write_catalog(self.translations, locale, _RU)
            self.svc.set_user_language(self.admin.id, locale)
        self._login()
        resp = self.client.get('/settings')
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    def test_settings_renders_russian(self):
        body = self._settings('ru')
        self.assertIn('🔄 Чтение и синхронизация', body)
        self.assertIn('Отличается от значений по умолчанию', body)
        self.assertIn('placeholder="Найти настройку…"', body)
        # {% trans %} with a url_for() variable: the link target survives translation.
        self.assertIn('в <a href="/settings#users" style="color:#aab4f5;">Настройки → Пользователи', body)
        # The JS updates #effective_session_gap in place, so the span keeps its id.
        self.assertRegex(body, r'промежуток <strong><span id="effective_session_gap">[\d.]+</span> мин\.</strong>')
        # Users table: the role badge is translated, the CSS class keeps the role key.
        self.assertIn('<span class="u-badge admin">администратор</span>', body)
        self.assertIn('<span class="u-badge on">активен</span>', body)
        self.assertIn('placeholder="новый пароль"', body)
        # Form field names and values stay English.
        self.assertIn('name="READING_SESSION_MERGE_MINUTES"', body)
        self.assertNotIn('🔄 Reading &amp; Sync', body)

    def test_settings_english_by_default(self):
        body = self._settings()
        self.assertIn('🔄 Reading &amp; Sync', body)
        self.assertIn('<span class="u-badge admin">admin</span>', body)
        self.assertRegex(body, r'The gap actually used is <strong><span id="effective_session_gap">[\d.]+</span> minutes</strong>')

    def test_markup_text_is_extracted(self):
        """Every visible text node in the markup part of settings.html is wrapped.

        Brand names, two-letter service badges, sample values and paths are
        allowed to stay as they are; anything else left bare would ship English.
        """
        src = _SETTINGS.read_text(encoding="utf-8")
        markup = src.split("<script>", 1)[0]
        markup = re.sub(r"\{#.*?#\}", "", markup, flags=re.S)
        markup = re.sub(r"\{%-?\s*trans\b.*?\{%-?\s*endtrans\s*-?%\}", "", markup, flags=re.S)
        markup = re.sub(r"\{\{.*?\}\}|\{%.*?%\}", "", markup, flags=re.S)
        markup = re.sub(r"<(style|textarea|pre)\b.*?</\1>", "", markup, flags=re.S)
        text = re.sub(r"<[^>]+>", "\n", markup)
        allowed = {
            "Audiobookshelf", "KOReader / KoSync", "Storyteller", "Grimmory", "BookFusion",
            "BookOrbit", "Kavita", "Calibre-Web Automated", "Hardcover", "StoryGraph",
            "CPU", "Ollama", "OpenAI", "Readest",
        }
        leftovers = []
        for line in text.split("\n"):
            line = re.sub(r"&\w+;", "", line).strip()
            if not re.search(r"[A-Za-z]{2}", line) or line in allowed or re.fullmatch(r"[A-Z]{2,3}", line):
                continue
            leftovers.append(line)
        self.assertEqual(leftovers, [])

    def test_extraction_finds_the_settings_strings(self):
        ids = {
            msg if isinstance(msg, str) else msg[0]
            for _, msg, _, _ in extract_from_file(
                "jinja2", str(_SETTINGS), options={"extensions": "jinja2.ext.i18n"})
        }
        for msgid in ("🔄 Reading &amp; Sync", _KOSYNC_NOTE, _SESSION_GAP, "admin", "user"):
            self.assertIn(msgid, ids)
        self.assertGreater(len(ids), 450)


if __name__ == "__main__":
    unittest.main()
