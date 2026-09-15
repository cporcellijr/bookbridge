"""Time helpers.

`datetime.utcnow()` is deprecated in Python 3.12+. This module provides a
drop-in replacement that preserves the previous behaviour exactly: a *naive*
UTC timestamp. The app stores these in timezone-naive SQLite DateTime columns
and compares them against each other, so returning naive (rather than aware)
datetimes keeps every existing comparison and column default working unchanged.
"""
from datetime import datetime, timezone


def utcnow() -> datetime:
    """Return the current UTC time as a naive datetime.

    Equivalent to the deprecated ``datetime.utcnow()`` but without the warning.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def datetime_to_epoch(value: datetime) -> float:
    """Return Unix epoch seconds, interpreting naive datetimes as UTC.

    BookBridge deliberately stores UTC datetimes without ``tzinfo`` in SQLite.
    Calling ``datetime.timestamp()`` directly on those values would interpret
    them in the host's local timezone. Attach UTC only for the conversion so the
    stored/comparison semantics stay naive while epoch values remain portable.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()
