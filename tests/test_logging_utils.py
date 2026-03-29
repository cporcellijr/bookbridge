import logging

from src.utils.logging_utils import MojibakeSafeFormatter, repair_mojibake


def _mojibake(text, rounds=1):
    value = text
    for _ in range(rounds):
        raw = value.encode("utf-8")
        for encoding in ("cp1252", "latin-1"):
            try:
                value = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            raise AssertionError("Unable to generate mojibake test fixture")
    return value


def test_repair_mojibake_repairs_single_pass_emoji_text():
    original = "\U0001F4DA Grimmory: Loaded 12 books from database"
    assert repair_mojibake(_mojibake(original)) == original


def test_repair_mojibake_repairs_single_pass_symbol_text():
    original = "\u26A0\uFE0F Grimmory: Cache refresh already in progress"
    assert repair_mojibake(_mojibake(original)) == original


def test_repair_mojibake_repairs_double_pass_symbol_text():
    original = "\u26A0\uFE0F Grimmory: Cache refresh already in progress"
    assert repair_mojibake(_mojibake(original, rounds=2)) == original


def test_mojibake_safe_formatter_repairs_formatted_log_output():
    formatter = MojibakeSafeFormatter("%(levelname)s - %(message)s")
    record = logging.LogRecord(
        name="test.logger",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=_mojibake("\U0001F504 Hardcover: Setting finished_at to '2026-03-28'"),
        args=(),
        exc_info=None,
    )

    assert formatter.format(record) == "INFO - \U0001F504 Hardcover: Setting finished_at to '2026-03-28'"
