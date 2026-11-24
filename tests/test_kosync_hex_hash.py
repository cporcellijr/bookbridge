"""_is_valid_hex_hash accepts exactly 32 hex digits, as KOReader sends."""

import pytest

from src.api.kosync_server import _is_valid_hex_hash

VALID = "0123456789abcdef0123456789abcdef"


@pytest.mark.parametrize("value", [VALID, VALID.upper(), "0123456789ABCDEF0123456789abcdef"])
def test_accepts_32_hex_digits(value):
    assert _is_valid_hex_hash(value)


@pytest.mark.parametrize("value", [
    "0x" + VALID[:30],
    "-" + VALID[:31],
    "+" + VALID[:31],
    VALID[:15] + "_" + VALID[:16],
    " " + VALID[:31],
    VALID[:31] + "\n",
    VALID[:31],
    VALID + "0",
    "g" + VALID[:31],
    "",
])
def test_rejects_anything_else(value):
    assert not _is_valid_hex_hash(value)
