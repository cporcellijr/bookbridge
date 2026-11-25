"""language_key: one comparable code for the language forms libraries actually return."""

import pytest

from src.utils.language_key import language_key


@pytest.mark.parametrize("value, expected", [
    # Live BookOrbit returned all three of these for English books (2026-11-24 probe).
    ("en", "en"), ("English", "en"), ("eng", "en"),
    ("en-GB", "en"), ("en_US", "en"), (" EN ", "en"),
    ("es", "es"), ("Spanish", "es"), ("spa", "es"),
    ("fre", "fr"), ("fra", "fr"), ("pt-BR", "pt"),
    ("", ""), (None, ""), ("und", ""), ("mul", ""),
    ("xx", "xx"), ("xx-YY", "xx"),
])
def test_language_key(value, expected):
    assert language_key(value) == expected
