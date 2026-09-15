"""Bounded estimates for progress-derived reading sessions (not player telemetry)."""

import math
import os

MAX_SESSION_SECONDS = 14400

# A closed session stops being retried once it has gone undelivered for this long,
# so a decommissioned destination cannot pin the queue or grow the buffer table without bound.
DELIVERY_RETRY_WINDOW_SECONDS = 7 * 86400


def _positive_number(value: object, default: float) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) and number > 0 else default
    except (TypeError, ValueError):
        return default


# The poll-derived floor exists so sparse scheduled observations do not split one
# sitting, but it must not run away: an install driven by instant sync can set a
# sync period of hours, and twice that would merge a week of reading into one
# session. Past this ceiling the configured value stands on its own.
MAX_DERIVED_GAP_MINUTES = 30

# The scheduler closes an idle session on a timer, but only a later observation
# can prove that the apparent idle was really uninterrupted reading a service had
# not flushed yet. Closing at exactly the gap denies that observation its vote —
# it arrives moments too late and starts a second session. So the timer waits
# this much longer than the gap, and the observation-time test decides.
IDLE_CLOSE_PATIENCE = 2

# The most idle time one observation's own progress may account for. BookOrbit
# was measured flushing every 37-50 minutes, so this must comfortably exceed
# that, while staying far below a break a reader would call a break.
MAX_EXPLAINED_IDLE_SECONDS = 5400


def effective_session_gap_seconds() -> float:
    """Read the idle gap, allowing two scheduled observations before splitting."""
    configured = _positive_number(os.environ.get("READING_SESSION_MERGE_MINUTES"), 5)
    poll = _positive_number(os.environ.get("SYNC_PERIOD_MINS"), 5)
    derived = min(2 * poll, MAX_DERIVED_GAP_MINUTES)
    return max(1, configured, derived) * 60


def uncovered_fraction(start_progress: float, end_progress: float,
                       covered: list[tuple[float, float]]) -> float:
    """Fraction of a progress span no existing session already covers.

    An aggregated session spans far more of a book than the per-observation
    sessions this replaced, so a destination that logs its own reading may cover
    part of ours without covering the reading we are about to report. Returns 1.0
    when nothing overlaps and 0.0 when the span is fully covered.
    """
    low, high = sorted((float(start_progress), float(end_progress)))
    width = high - low
    if width <= 0:
        return 0.0
    clipped = sorted(
        (max(low, min(a, b)), min(high, max(a, b))) for a, b in covered
    )
    covered_width = 0.0
    reached = low
    for span_start, span_end in clipped:
        if span_end <= reached:
            continue
        covered_width += span_end - max(span_start, reached)
        reached = span_end
    return max(0.0, min(1.0, (width - covered_width) / width))


def movement_seconds(position_delta: float, now: float, previous_at: float | None,
                     gap_seconds: float) -> float:
    """Bound a positive movement by observed time; unknown first intervals are estimates.

    Position alone cannot distinguish seeks, playback speeds, or pauses within an
    observation interval. Slow playback is undercounted; a first observation with
    no baseline may overcount fast playback. Never manufacture a minimum duration.

    The idle gap bounds only the first interval, where there is no baseline to
    measure against. It must NOT bound a measured interval: upstream services
    flush progress on their own schedule, so one observation can legitimately
    cover an hour of listening, and capping it at the gap silently discards the
    rest (observed live: 3973s of audio credited as exactly 1800s).
    """
    if not math.isfinite(position_delta) or position_delta <= 0:
        return 0.0
    if previous_at is None:
        return min(position_delta, gap_seconds, MAX_SESSION_SECONDS)
    return min(position_delta, max(0.0, now - previous_at), MAX_SESSION_SECONDS)


def unexplained_idle_seconds(elapsed: float, position_delta: float) -> float:
    """Idle time an observation's own progress does not account for.

    Whether a sitting ended is a question about *idle* time, not about how long
    we waited to hear about it. When a service flushes progress infrequently, a
    long wait followed by a large position jump is continuous reading, not a
    break — the jump is the evidence. Only the part of the wait that the reading
    cannot explain counts towards splitting the session.

    Bounded, because a large forward seek is indistinguishable from listening at
    the same rate: without a ceiling, someone who broke for three hours and then
    skipped four hours ahead would look like one continuous sitting. The ceiling
    sits well above any realistic flush cadence and well below a real break.
    """
    if not math.isfinite(position_delta) or position_delta <= 0:
        explained = 0.0
    else:
        explained = min(position_delta, MAX_EXPLAINED_IDLE_SECONDS)
    return max(0.0, elapsed - explained)
