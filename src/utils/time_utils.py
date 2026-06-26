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
