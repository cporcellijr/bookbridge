"""One comparable code for a book's language, whatever form its source wrote it in.

Libraries disagree: BookOrbit alone returns ``en``, ``eng`` and ``English`` for English
books, and EPUBs carry BCP 47 tags such as ``en-GB``. Comparing those strings directly
calls two English editions a language mismatch.
"""

from functools import lru_cache

from babel import Locale, UnknownLocaleError

# Codes that say "no particular language"; Babel would otherwise guess one (und -> en).
_NO_LANGUAGE = {"", "und", "mul", "zxx", "mis"}


@lru_cache(maxsize=1)
def _english_names() -> dict:
    return {name.casefold(): code for code, name in Locale("en").languages.items()}


@lru_cache(maxsize=1024)
def _key(text: str) -> str:
    folded = text.casefold()
    if folded in _NO_LANGUAGE:
        return ""
    try:
        return Locale.parse(text.replace("-", "_")).language
    except (UnknownLocaleError, ValueError, TypeError):
        pass
    return _english_names().get(folded) or folded.replace("_", "-").split("-")[0]


def language_key(value: object) -> str:
    """``'en'``, ``'en-GB'``, ``'eng'`` and ``'English'`` all give ``'en'``; blank or undetermined gives ``''``.

    A value Babel cannot read keeps the old comparison: its lower-cased primary subtag.
    """
    return _key(str(value or "").strip())
