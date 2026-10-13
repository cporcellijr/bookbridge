import logging
import threading
import time
import os
from datetime import datetime
from pathlib import Path
from logging.handlers import RotatingFileHandler
from functools import wraps
from typing import Any, Optional
import requests

logger = logging.getLogger(__name__)


MOJIBAKE_MARKERS = (
    "\u00f0\u0178",  # Common emoji mojibake prefix, e.g. ðŸ
    "\u00e2",        # Common punctuation/symbol mojibake prefix, e.g. âš, âœ, â
    "\u00c3",        # Common accented-text mojibake prefix, e.g. Ã
    "\u00ef\u00b8",  # Variation selector mojibake, e.g. ï¸
)


def repair_mojibake(text):
    """Repair UTF-8 text that was accidentally decoded as a single-byte encoding."""
    if not isinstance(text, str) or not any(marker in text for marker in MOJIBAKE_MARKERS):
        return text

    repaired = text
    for _ in range(3):
        if not any(marker in repaired for marker in MOJIBAKE_MARKERS):
            break

        raw_bytes = bytearray()
        for char in repaired:
            codepoint = ord(char)
            if codepoint <= 0xFF:
                raw_bytes.append(codepoint)
                continue

            try:
                raw_bytes.extend(char.encode("cp1252"))
            except UnicodeError:
                raw_bytes = None
                break

        if raw_bytes is None:
            break

        try:
            candidate = bytes(raw_bytes).decode("utf-8")
        except UnicodeError:
            break

        if not candidate or candidate == repaired:
            break
        repaired = candidate

    return repaired


class MojibakeSafeFormatter(logging.Formatter):
    """Formatter that normalizes mojibake before logs are emitted."""

    def format(self, record):
        return repair_mojibake(super().format(record))


class ThresholdExceptionFormatter(logging.Formatter):
    """Formatter that renders exception/traceback text only at or above a level threshold.

    Records below ``threshold`` (default ``logging.ERROR``) that carry
    ``exc_info``, ``exc_text``, or ``stack_info`` are rendered as a plain
    message line, without the traceback block a plain :class:`logging.Formatter`
    would otherwise append. Records at or above ``threshold`` format exactly as
    :class:`logging.Formatter` does today.

    The record's ``exc_info``/``exc_text``/``stack_info`` attributes are always
    restored before ``format()`` returns, even when suppressed for this
    handler's own output -- other handlers processing the same ``LogRecord``
    afterwards (e.g. the diagnostics collector) must still see the original
    exception data.
    """

    def __init__(
        self,
        fmt: Optional[str] = None,
        datefmt: Optional[str] = None,
        style: str = '%',
        threshold: int = logging.ERROR,
        **kwargs: Any,
    ) -> None:
        super().__init__(fmt=fmt, datefmt=datefmt, style=style, **kwargs)
        self.threshold = threshold

    def format(self, record: logging.LogRecord) -> str:
        suppress = record.levelno < self.threshold and bool(
            record.exc_info or record.exc_text or record.stack_info
        )
        if not suppress:
            return super().format(record)

        exc_info = record.exc_info
        exc_text = record.exc_text
        stack_info = record.stack_info
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        try:
            return super().format(record)
        finally:
            record.exc_info = exc_info
            record.exc_text = exc_text
            record.stack_info = stack_info


class ThresholdMojibakeFormatter(MojibakeSafeFormatter, ThresholdExceptionFormatter):
    """Formatter combining mojibake repair with threshold-gated exception rendering.

    Used by the console and file handlers so a below-threshold record with
    ``exc_info`` (e.g. ``logger.warning(..., exc_info=True)``) prints as a
    single repaired line, while ERROR+ records still render the full
    traceback exactly as before.
    """


class MemoryLogHandler(logging.Handler):
    """Log handler that keeps logs in memory for real-time streaming."""

    def __init__(self, maxlen=1000):
        super().__init__()
        self.logs = []
        self.maxlen = maxlen

    def emit(self, record):
        try:
            log_entry = {
                'timestamp': datetime.fromtimestamp(record.created).strftime('%Y-%m-%d %H:%M:%S'),
                'level': record.levelname,
                'message': repair_mojibake(record.getMessage()),
                'module': record.name
            }
            self.logs.append(log_entry)
            # Keep only the most recent logs
            if len(self.logs) > self.maxlen:
                self.logs.pop(0)
        except Exception:
            pass

    def get_recent_logs(self, count=100):
        """Get the most recent logs up to specified count."""
        return self.logs[-count:] if len(self.logs) > count else self.logs.copy()


def setup_file_logging():
    """Setup file logging handler."""
    DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
    if not DATA_DIR.exists():
        logger.warning("⚠️ Not setting up file logging because missing data dir")
        return ""

    LOG_DIR = DATA_DIR / "logs"
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    LOG_PATH = LOG_DIR / "unified_app.log"
    log_level = getattr(logging, os.environ.get('LOG_LEVEL', 'INFO').upper(), logging.INFO)

    file_handler = RotatingFileHandler(str(LOG_PATH), maxBytes=10*1024*1024, backupCount=5, encoding='utf-8')
    file_handler.setLevel(log_level)
    file_handler.setFormatter(ThresholdMojibakeFormatter('[%(asctime)s] %(levelname)s - %(name)s: %(message)s'))

    # Attach to the root logger so all module loggers go to the same file
    root_logger = logging.getLogger()
    root_logger.addHandler(file_handler)

    return LOG_PATH


def setup_console_logging():
    """Setup console logging handler."""
    console_handler = logging.StreamHandler()
    # Use LOG_LEVEL env variable or fallback to INFO
    log_level = getattr(logging, os.environ.get('LOG_LEVEL', 'INFO').upper(), logging.INFO)
    console_handler.setLevel(log_level)
    console_handler.setFormatter(ThresholdMojibakeFormatter('%(asctime)s - %(levelname)s - %(message)s'))

    # Add to root logger
    root_logger = logging.getLogger()
    root_logger.addHandler(console_handler)

    # Set root logger to DEBUG so all messages reach handlers, let handlers filter individually
    root_logger.setLevel(logging.DEBUG)

    # Prevent Werkzeug from propagating its logs up to the root logger (avoids duplicate access lines)
    werkzeug_logger = logging.getLogger('werkzeug')
    werkzeug_logger.propagate = False
    werkzeug_logger.setLevel(logging.WARNING)

    # Mark that we've already configured logging to prevent basicConfig from running
    root_logger._configured = True


def setup_memory_logging():
    """Setup memory log handler to capture logs from all modules."""
    # Create and configure memory log handler
    memory_handler = MemoryLogHandler()
    memory_handler.setLevel(logging.DEBUG)

    # Add to root logger to capture all logs from all modules
    root_logger = logging.getLogger()
    root_logger.addHandler(memory_handler)

    return memory_handler


class TelegramHandler(logging.Handler):
    """Log handler that sends logs to a Telegram chat via bot API."""
    def __init__(self, bot_token, chat_id, min_level=logging.ERROR):
        super().__init__(min_level)
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    def emit(self, record):
        # Prevent infinite loops - don't log failures from this handler itself
        if record.name == __name__ and 'TelegramHandler' in record.getMessage():
            return

        try:
            message = self.format(record)
            payload = {
                'chat_id': self.chat_id,
                'text': message,
                'parse_mode': 'HTML',
                'disable_notification': False
            }
            response = requests.post(self.api_url, data=payload, timeout=5)
            response.raise_for_status()  # Raise exception for HTTP errors
        except Exception as e:
            # Log telegram handler failures without causing loops
            logger.error(f"TelegramHandler failed to send message: {str(e)}", exc_info=True)  # Never raise from logging


def setup_telegram_logging():
    """Setup Telegram logging handler if environment variables are set."""
    bot_token = os.environ.get('TELEGRAM_BOT_TOKEN')
    chat_id = os.environ.get('TELEGRAM_CHAT_ID')
    log_level_name = os.environ.get('TELEGRAM_LOG_LEVEL', 'ERROR').upper()
    log_level = getattr(logging, log_level_name, logging.ERROR)

    enabled_val = os.environ.get("TELEGRAM_ENABLED", "").lower()
    if enabled_val == 'false':
        return None

    if not bot_token or not chat_id:
        return None

    logger.info("Setting up telegram logger")
    handler = TelegramHandler(bot_token, chat_id, min_level=log_level)
    handler.setFormatter(MojibakeSafeFormatter(
        '<b>[%(asctime)s]</b> <code>%(levelname)s</code> - <b>%(name)s</b>: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'))
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)
    return handler


def sanitize_log_data(data):
    """Truncate long strings to "First 50... [truncated] ...Last 50"."""
    if data is None:
        return ""
    try:
        s = str(data)
    except Exception:
        return "[unrepresentable]"
    if len(s) <= 100:
        return s
    return f"{s[:50]}... [truncated] ...{s[-50:]}"


def time_execution(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        start = time.time()
        result = func(*args, **kwargs)
        end = time.time()
        ms = int((end - start) * 1000)
        try:
            logger.info(f"⏱️ [{func.__name__}] took {ms}ms")
        except Exception:
            pass
        return result
    return wrapper


class PersistentConditionLogger:
    """Suppresses repeat log noise for a condition that stays true across many
    calls (a dead retry loop, a standing config gap, a hot caller bug).

    Without this, a warning inside a loop that never clears re-logs the exact
    same line every attempt/cycle, drowning out everything else. With it, the
    first occurrence of a given ``key`` is logged exactly as it always was
    (byte-identical, at WARNING — the greppable contract issue reporters rely
    on is preserved), later occurrences drop to DEBUG, and every ``every``-th
    occurrence re-surfaces at WARNING so a still-broken condition doesn't go
    completely silent. Call :meth:`resolve` from the matching success path to
    announce recovery once and reset the counter.

    Thread-safe via a single lock guarding the counter dict. Counter state is
    process-lifetime (reset on restart) — it is not persisted, by design.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}

    def warn(
        self,
        logger: logging.Logger,
        key: str,
        message: str,
        *args: Any,
        every: int = 50,
        level: int = logging.WARNING,
        **kwargs: Any,
    ) -> None:
        """Log a persistent-condition warning, suppressing repeats for ``key``.

        Count 1 (first occurrence for this ``key``): logs ``message`` via
        ``logger.log(level, ...)`` completely unchanged — same text, same
        ``*args``/``**kwargs`` (so ``exc_info=True`` still attaches a
        traceback).

        Every ``every``-th occurrence thereafter (count % every == 0): logs
        ``message`` again via ``logger.log(level, ...)``, with
        ``" (occurrence %d; repeats logged at DEBUG)"`` appended so the
        condition stays visible without repeating every single time.

        All other occurrences: logs ``message`` via ``logger.debug`` (still
        reachable for local troubleshooting, but out of the fleet-noisy path)
        regardless of ``level``.

        Args:
            logger: The module logger to emit through.
            key: Identity of the persistent condition (e.g. a host, user id,
                or a fixed string for a caller-bug site with no natural key).
            message: The log message. Passed through unchanged on count 1.
            *args: Positional args forwarded to the underlying logger call
                (supports both %-style lazy logging args and plain messages).
            every: Repeat-warning interval. Must be >= 1 (clamped to 1).
            level: Severity for the first and every-``every``-th loud
                emissions (default ``logging.WARNING``). Pass
                ``logging.ERROR`` for sites where the original, pre-helper
                severity must be preserved (e.g. for a downstream handler
                threshold like Telegram alerting).
            **kwargs: Forwarded to the underlying logger call (e.g. exc_info).
        """
        if every < 1:
            every = 1

        with self._lock:
            count = self._counts.get(key, 0) + 1
            self._counts[key] = count

        if count == 1:
            logger.log(level, message, *args, **kwargs)
        elif count % every == 0:
            logger.log(
                level,
                f"{message} (occurrence {count}; repeats logged at DEBUG)",
                *args,
                **kwargs,
            )
        else:
            logger.debug(message, *args, **kwargs)

    def resolve(
        self,
        logger: logging.Logger,
        key: str,
        message: str,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        """Announce recovery for ``key`` and reset its counter.

        If ``key`` has a nonzero occurrence count (i.e. :meth:`warn` fired for
        it at least once since the last resolve/reset), logs ``message`` via
        ``logger.info`` with ``" (recovered after N occurrences)"`` appended,
        then resets the counter to 0. If ``key`` has no recorded occurrences,
        this is a silent no-op — there is nothing to announce recovery from.

        Args:
            logger: The module logger to emit through.
            key: Same identity used in the matching :meth:`warn` calls.
            message: A NEW recovery message (never the original warning text).
            *args: Forwarded to the underlying ``logger.info`` call.
            **kwargs: Forwarded to the underlying ``logger.info`` call.
        """
        with self._lock:
            count = self._counts.get(key, 0)
            if count == 0:
                return
            self._counts[key] = 0

        occurrences = "1 occurrence" if count == 1 else f"{count} occurrences"
        logger.info(f"{message} (recovered after {occurrences})", *args, **kwargs)

    def reset(self) -> None:
        """Clear all tracked counters. Test-only."""
        with self._lock:
            self._counts.clear()


_persistent_condition_logger: Optional["PersistentConditionLogger"] = None
_persistent_condition_logger_lock = threading.Lock()


def get_persistent_condition_logger() -> "PersistentConditionLogger":
    """Return the process-wide :class:`PersistentConditionLogger` singleton."""
    global _persistent_condition_logger
    with _persistent_condition_logger_lock:
        if _persistent_condition_logger is None:
            _persistent_condition_logger = PersistentConditionLogger()
        return _persistent_condition_logger


# Global instances - initialize when module is imported (BEFORE main.py)
LOG_PATH = setup_file_logging()  # Setup file logging first
setup_console_logging()  # Setup console logging second
memory_log_handler = setup_memory_logging()  # Then memory logging
telegram_log_handler = setup_telegram_logging()  # Optionally setup Telegram logging
