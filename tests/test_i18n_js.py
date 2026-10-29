"""i18n: strings built in JavaScript (#516, JS step).

Inline <script> strings come from {{ _('...')|tojson }}; counts from ngettext
through plural_forms() in _i18n.html; static/js strings through bbT() with the
translations passed by index.html. These tests check the pieces fit: the
rendered forms in a real Russian catalog, the JS helpers' plural choice, and
that every bbT() key in static/js has a translation entry.
"""

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from tests.test_i18n import I18nTestBase, _write_catalog

ROOT = Path(__file__).parent.parent
TEMPLATES = ROOT / "templates"
STATIC_JS = ROOT / "static" / "js"


def _bbt_keys(source: str) -> set:
    return {m.group(1).replace("\\'", "'") for m in re.finditer(r"bbT\('((?:\\'|[^'])*)'", source)}


class StaticJsStringsTests(unittest.TestCase):
    def test_every_bbt_key_has_a_translation_entry(self):
        index = (TEMPLATES / "index.html").read_text(encoding="utf-8")
        block = index.split("window.BB_STRINGS = Object.assign", 1)[1].split("</script>", 1)[0]
        entries = {m.group(1).replace("\\'", "'") for m in re.finditer(r"^\s*'((?:\\'|[^'])*)': _\(", block, re.M)}
        entries |= {m.group(1) for m in re.finditer(r"window\.BB_STRINGS\['([^']*)'\]", block)}
        keys = set()
        for path in STATIC_JS.glob("*.js"):
            keys |= _bbt_keys(path.read_text(encoding="utf-8"))
        self.assertTrue(keys)
        self.assertEqual(sorted(keys - entries), [], "bbT() keys without a translation in index.html")
        self.assertEqual(sorted(entries - keys), [], "translation entries no static/js file uses")

    def test_no_bare_english_in_static_js_ui_assignments(self):
        bare = re.compile(r"""(?:textContent|innerText|title)\s*=\s*['"`][^'"`]*[A-Za-z]{3}""")
        for path in STATIC_JS.glob("*.js"):
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                self.assertIsNone(bare.search(line), f"{path.name}:{lineno}: {line.strip()}")


class PluralHelperTests(unittest.TestCase):
    """bbPlural from base.html picks the right Russian form for any count."""

    def test_bbplural_russian_forms(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is required to run the JS helpers")
        base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
        helpers = base.split("<script>", 1)[1].split("</script>", 1)[0]
        script = helpers + """
            const forms = { one: '{count} книга', few: '{count} книги', many: '{count} книг', other: '{count} книги' };
            const out = [1, 2, 5, 11, 21, 22, 25, 101].map(n => window.bbPlural(forms, n));
            out.push(window.bbFormat('{a} из {b}', { a: 3, b: 7 }));
            out.push(window.bbFormat('{missing} stays', {}));
            out.push(window.bbT('Absent key {x}', { x: 1 }));
            console.log(JSON.stringify(out));
        """
        prelude = "const window = globalThis; const document = { documentElement: { lang: 'ru' } };\n"
        result = subprocess.run([node, "-e", prelude + script], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [
            "1 книга", "2 книги", "5 книг", "11 книг", "21 книга", "22 книги", "25 книг", "101 книга",
            "3 из 7", "{missing} stays", "Absent key 1",
        ])


_RU = {
    ('Showing: {count} line', 'Showing: {count} lines'): (
        'Показано: {count} строка', 'Показано: {count} строки', 'Показано: {count} строк'),
    ('%(num)s item', '%(num)s items'): ('%(num)s элемент', '%(num)s элемента', '%(num)s элементов'),
    'Switching filter...': 'Смена фильтра...',
    'No logs found matching current filters': 'Нет записей, подходящих под фильтры',
    'Copy failed': 'Не удалось скопировать',
}


def _write_plural_catalog(directory, messages):
    """Like _write_catalog, but with Russian plural forms."""
    from babel.messages.catalog import Catalog
    from babel.messages.mofile import write_mo
    lc_dir = Path(directory) / "ru" / "LC_MESSAGES"
    lc_dir.mkdir(parents=True, exist_ok=True)
    catalog = Catalog(locale="ru", domain="messages")
    for msgid, msgstr in messages.items():
        catalog.add(msgid, string=msgstr)
    with open(lc_dir / "messages.mo", "wb") as fh:
        write_mo(fh, catalog)
    from src.web_server import available_locales
    available_locales.cache_clear()


class RenderedJsStringsTests(I18nTestBase):
    def _page(self, path, locale=None):
        if locale:
            _write_plural_catalog(self.translations, _RU)
            self.svc.set_user_language(self.admin.id, locale)
        self._login()
        resp = self.client.get(path)
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    @staticmethod
    def _js_const(body, name):
        match = re.search(r"const %s = (\{.*?\});" % re.escape(name), body)
        assert match, name
        return json.loads(match.group(1))

    def test_plural_forms_render_all_russian_forms(self):
        body = self._page('/logs', 'ru')
        self.assertEqual(self._js_const(body, 'LINES_SHOWN'), {
            'one': 'Показано: {count} строка',
            'few': 'Показано: {count} строки',
            'many': 'Показано: {count} строк',
            'other': 'Показано: {count} строки',
        })
        # tojson writes non-ASCII as \\uXXXX escapes, which JS decodes the same way.
        match = re.search(r"showFilterPending\('switch', (\"[^\"]*\")\)", body)
        self.assertIsNotNone(match)
        self.assertEqual(json.loads(match.group(1)), 'Смена фильтра...')

    def test_reused_server_msgid_keeps_placeholder(self):
        # suggestions.html reuses ngettext('%(num)s item', ...) with num='{count}',
        # so the JS gets the placeholder, not a baked-in sample number.
        body = self._page('/suggestions', 'ru')
        forms = self._js_const(body, 'ITEM_COUNT')
        self.assertEqual(forms['one'], '{count} элемент')
        self.assertEqual(forms['few'], '{count} элемента')
        self.assertEqual(forms['many'], '{count} элементов')

    def test_english_forms_by_default(self):
        body = self._page('/logs')
        self.assertEqual(self._js_const(body, 'LINES_SHOWN'), {
            'one': 'Showing: {count} line', 'few': 'Showing: {count} lines',
            'many': 'Showing: {count} lines', 'other': 'Showing: {count} lines',
        })

    def test_logs_filter_pending_is_checked_by_marker_not_text(self):
        source = (TEMPLATES / "logs.html").read_text(encoding="utf-8")
        script = source.split("<script>", 1)[1]
        self.assertNotIn("includes('Switching filter')", script)
        self.assertNotIn("includes('Applying filter')", script)
        self.assertIn("loading.dataset.pending = kind;", script)


if __name__ == "__main__":
    unittest.main()
