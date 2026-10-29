"""i18n: the Russian catalog shipped in translations/ (#516).

Kept apart from tests/test_i18n.py so catalog updates and template PRs do not
edit the same file.
"""

import unittest
from pathlib import Path

from babel.messages.mofile import write_mo

from tests.test_i18n import I18nTestBase


_RU_PO = Path(__file__).parent.parent / "translations" / "ru" / "LC_MESSAGES" / "messages.po"


def _compile_shipped_ru(directory: str) -> None:
    """Compile the repo's ru .po into <directory>/ru/LC_MESSAGES/messages.mo."""
    from babel.messages.pofile import read_po

    with open(_RU_PO, 'rb') as fh:
        catalog = read_po(fh, locale='ru')
    lc_dir = Path(directory) / 'ru' / 'LC_MESSAGES'
    lc_dir.mkdir(parents=True, exist_ok=True)
    with open(lc_dir / 'messages.mo', 'wb') as fh:
        write_mo(fh, catalog)
    from src.web_server import available_locales
    available_locales.cache_clear()


class ShippedCatalogTests(unittest.TestCase):
    """The ru catalog in the repo must not break a page once it is compiled.

    Only translated entries are checked: a new or fuzzy entry falls back to
    English, so template changes never make this test fail on their own.
    """

    def test_ru_translations_keep_placeholders_and_format(self):
        import re
        from collections import defaultdict
        from babel.messages.pofile import read_po

        with open(_RU_PO, 'rb') as fh:
            catalog = read_po(fh, locale='ru')
        self.assertEqual(catalog.num_plurals, 3)
        # %(name)s for Jinja/Babel, {name} for strings filled in JavaScript by bbFormat().
        placeholders = lambda s: sorted(re.findall(r'%\(\w+\)s|%%|\{\w+\}', s))
        bad = []
        for message in catalog:
            if not message.id or message.fuzzy:
                continue
            ids = message.id if isinstance(message.id, tuple) else (message.id,)
            strings = message.string if isinstance(message.string, tuple) else (message.string,)
            for string in strings:
                if not string:
                    continue
                if placeholders(string) != placeholders(ids[0]):
                    bad.append(f"placeholders differ: {ids[0]!r} -> {string!r}")
                try:
                    string % defaultdict(str)
                except (ValueError, TypeError, KeyError) as exc:
                    bad.append(f"breaks newstyle gettext: {string!r} ({exc})")
        bad += [f"{message.id!r}: {errors}" for message, errors in catalog.check()]
        self.assertEqual(bad, [], "\n".join(bad))


class ShippedCatalogRenderTests(I18nTestBase):
    def test_shipped_ru_catalog_renders(self):
        _compile_shipped_ru(self.translations)
        from src.web_server import available_locales
        self.assertIn('ru', available_locales())
        self._login()
        self.client.post('/account/language', data={'language': 'ru'}, follow_redirects=False)
        body = self.client.get('/account').get_data(as_text=True)
        self.assertIn('<html lang="ru"', body)
        self.assertIn('Язык интерфейса', body)
        self.assertIn('Библиотека', body)
        body = self.client.get('/suggestions').get_data(as_text=True)
        self.assertIn('id="queueCount">0 элементов</span>', body)


if __name__ == '__main__':
    unittest.main()
