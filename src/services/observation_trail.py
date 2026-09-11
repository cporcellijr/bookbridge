"""
Observation trail — a short, per-client history of externally originated positions.

A single backward position report is ambiguous. It can be a deliberate rewind (the
user went back to where they fell asleep), a stale client replaying an old position,
BookBridge's own write-back echoing, or a locator that collapsed to start-of-book
(#420). Leader selection cannot tell them apart from one sample, so today it guesses:
it demotes a text client that moves backward (issue #215) and obeys an audio client
that does the same (the ABS -> KoSync case in the same thread).

What separates them is what happens NEXT. A reader who genuinely rewound keeps
reading from the new point, so that client emits a SEQUENCE of positions advancing
from the rewind anchor. A stale or echoed report is a single anomalous sample that
does not advance.

This module records that sequence. It stores only observations that came from
OUTSIDE BookBridge — a device PUT, a poll that saw real movement, a socket event —
never our own write-back, which is what keeps a write-back from corroborating itself
(#413/#416).

It is a pure recorder: nothing here decides anything. `SyncManager` reads the trail
to judge whether a backward mover is corroborated.

Sources: 'put' (KoSync device PUT), 'poll' (ClientPoller fingerprint change),
'socket' (ABS Socket.IO progress event).
"""

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import List, Optional

logger = logging.getLogger(__name__)

# (client, abs_id, user) -> list of Observation, oldest first.
_trails: dict[str, List["Observation"]] = {}
_trails_lock = threading.Lock()

# How long an observation stays usable as corroboration. Matched to the existing
# KoSync recent-external-PUT horizon so the two agree about what "recent" means.
_DEFAULT_TRAIL_TTL_SECONDS = 600

# Hard cap per key. A trail only ever answers "did this client keep moving", so a
# handful of points is plenty and the bound keeps a chatty poller from growing it
# without limit.
_MAX_TRAIL_ENTRIES = 12

# GC horizon, deliberately longer than the TTL so a reader enforcing its own window
# is never starved by cleanup (same discipline as write_tracker's retention).
_MAX_RETENTION_SECONDS = 3600


@dataclass(frozen=True)
class Observation:
    """One externally originated position report."""
    timestamp: float
    pct: Optional[float]
    source: str
    device: str = ""
    normalized_ts: Optional[float] = None


def trail_ttl_seconds() -> int:
    """Read per call so the settings UI applies without a restart."""
    try:
        return max(0, int(os.environ.get("SYNC_OBSERVATION_TRAIL_SECONDS", "600") or _DEFAULT_TRAIL_TTL_SECONDS))
    except (TypeError, ValueError):
        return _DEFAULT_TRAIL_TTL_SECONDS


def _resolve_uid(user_id):
    """Key on the ambient sync user when a caller deep in a client cannot thread one
    through, so one user's reading never corroborates another's."""
    if user_id is not None:
        return user_id
    try:
        from src.utils.user_context import get_current_user_id
        return get_current_user_id()
    except Exception:
        return None


def _key(client_name: str, abs_id: str, user_id=None) -> str:
    return f"{_resolve_uid(user_id)}::{client_name}::{abs_id}"


def _cleanup_stale_locked(now: float) -> None:
    stale = [
        key for key, entries in _trails.items()
        if not entries or now - entries[-1].timestamp > _MAX_RETENTION_SECONDS
    ]
    for key in stale:
        del _trails[key]


def record_observation(
    client_name: str,
    abs_id: str,
    pct: Optional[float],
    source: str,
    user_id=None,
    device: str = "",
    normalized_ts: Optional[float] = None,
) -> None:
    """Append one externally originated observation.

    Callers must already have excluded BookBridge's own writes
    (`write_tracker.is_own_write`) — this records whatever it is given.
    """
    if not client_name or not abs_id:
        return
    now = time.time()
    observation = Observation(
        timestamp=now,
        pct=None if pct is None else float(pct),
        source=source,
        device=device or "",
        normalized_ts=normalized_ts,
    )
    with _trails_lock:
        _cleanup_stale_locked(now)
        entries = _trails.setdefault(_key(client_name, abs_id, user_id), [])
        # A client re-reporting the identical position is not new evidence that the
        # reader is moving, so collapse it onto the existing point.
        if entries and entries[-1].pct is not None and observation.pct is not None:
            if abs(entries[-1].pct - observation.pct) < 1e-9:
                entries[-1] = observation
                return
        entries.append(observation)
        if len(entries) > _MAX_TRAIL_ENTRIES:
            del entries[:-_MAX_TRAIL_ENTRIES]


def get_trail(client_name: str, abs_id: str, user_id=None, ttl_seconds: Optional[int] = None) -> List[Observation]:
    """Observations still inside the TTL, oldest first."""
    if not client_name or not abs_id:
        return []
    ttl = trail_ttl_seconds() if ttl_seconds is None else ttl_seconds
    if ttl <= 0:
        return []
    cutoff = time.time() - ttl
    with _trails_lock:
        entries = list(_trails.get(_key(client_name, abs_id, user_id), ()))
    return [entry for entry in entries if entry.timestamp >= cutoff]


def clear(client_name: str = None, abs_id: str = None, user_id=None) -> None:
    """Drop one trail, or all of them when called with no arguments (tests)."""
    with _trails_lock:
        if client_name and abs_id:
            _trails.pop(_key(client_name, abs_id, user_id), None)
        elif client_name is None and abs_id is None:
            _trails.clear()


@dataclass(frozen=True)
class Corroboration:
    """Whether a client's recent trail shows it genuinely moving on its own."""
    observations: int
    advancing: int
    sources: tuple
    span_seconds: float
    corroborated: bool
    reason: str

    def describe(self) -> str:
        return (
            f"trail={self.observations} obs over {self.span_seconds:.0f}s "
            f"({','.join(self.sources) or 'none'}), advancing={self.advancing}, "
            f"corroborated={self.corroborated} ({self.reason})"
        )


def required_observations() -> int:
    """How many independent advancing observations count as corroboration."""
    try:
        return max(2, int(os.environ.get("SYNC_REWIND_CORROBORATION_COUNT", "2") or 2))
    except (TypeError, ValueError):
        return 2


def evaluate(
    client_name: str,
    abs_id: str,
    anchor_pct: Optional[float] = None,
    user_id=None,
) -> Corroboration:
    """Judge whether `client_name`'s trail shows sustained independent movement.

    `anchor_pct` is the position under suspicion — the backward one. Movement is
    only corroborating when it advances FROM that anchor: a client that jumped back
    and then kept reading is a rewind, while one that jumped back and sat still, or
    snapped forward to where it already was, is not.
    """
    trail = get_trail(client_name, abs_id, user_id=user_id)
    needed = required_observations()
    sources = tuple(entry.source for entry in trail)
    span = (trail[-1].timestamp - trail[0].timestamp) if len(trail) > 1 else 0.0

    if len(trail) < needed:
        return Corroboration(
            observations=len(trail), advancing=0, sources=sources, span_seconds=span,
            corroborated=False, reason=f"only {len(trail)} observation(s), need {needed}",
        )

    # Count points that advance on the previous one while staying at or ahead of the
    # anchor. Reading forward from the rewind point is the signal.
    advancing = 0
    previous = None
    for entry in trail:
        if entry.pct is None:
            continue
        if anchor_pct is not None and entry.pct < anchor_pct - 1e-9:
            previous = entry.pct
            continue
        if previous is not None and entry.pct > previous + 1e-9:
            advancing += 1
        previous = entry.pct

    if advancing >= needed - 1 and len(trail) >= needed:
        return Corroboration(
            observations=len(trail), advancing=advancing, sources=sources, span_seconds=span,
            corroborated=True, reason=f"{advancing} advancing step(s) from the anchor",
        )
    return Corroboration(
        observations=len(trail), advancing=advancing, sources=sources, span_seconds=span,
        corroborated=False, reason=f"only {advancing} advancing step(s) from the anchor",
    )
