import logging

from src.utils.logging_utils import MojibakeSafeFormatter, repair_mojibake, setup_telegram_logging


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


def test_telegram_alert_with_angle_brackets_is_sent_as_plain_text(monkeypatch):
    """'<Response [500]>' or a traceback's '<lambda>' made Telegram reject HTML mode, losing the alert."""
    from unittest.mock import MagicMock

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    monkeypatch.delenv("TELEGRAM_ENABLED", raising=False)
    post = MagicMock()
    monkeypatch.setattr("src.utils.logging_utils.requests.post", post)
    handler = setup_telegram_logging()
    try:
        record = logging.LogRecord(
            name="test.logger", level=logging.ERROR, pathname=__file__, lineno=1,
            msg="ABS push failed: <Response [500]> " + "x" * 5000, args=(), exc_info=None,
        )
        handler.emit(record)
    finally:
        logging.getLogger().removeHandler(handler)

    payload = post.call_args.kwargs["data"]
    assert "parse_mode" not in payload
    assert "<Response [500]>" in payload["text"]
    assert len(payload["text"]) <= 4096
