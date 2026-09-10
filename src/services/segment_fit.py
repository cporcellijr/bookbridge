"""Segment fitting for out-of-order narration (issue #426, Phase 0).

Pure functions that fit each ebook chapter/section ("boundary") independently to
the audio timeline, instead of forcing every candidate anchor into one global
monotonic chain. This is the RANSAC-per-segment consensus step described in
``docs/PLAN_OUT_OF_ORDER_NARRATION.md``: it exists so a book whose EPUB spine
order differs from its audiobook's narration order (Four Past Midnight: four
novellas spined 2-4-3-1 but narrated 1-2-3-4) can still place every section
correctly, where a single global longest-increasing-subsequence filter can only
ever keep anchors from one or two of the sections.

No DB access, no I/O, no torch, no network, and no imports from
``src.services.alignment_service`` or ``src.db`` — this module is unit-testable
standalone. **Phase 0 is deliberately dead code**: nothing in the application
imports or calls it yet. Wiring it in behind a setting is Phase 2 of the plan.

``anchors`` is a list of candidate anchors as produced by
``AlignmentService._find_anchors``: ``{"ts": float, "char": int, "t_idx": int,
"b_idx": int}``. Some callers may carry ``global_char`` instead of ``char`` (see
`map_quality.point_char`); `_anchor_char` resolves either.
"""

import random
from dataclasses import dataclass, replace
from typing import Dict, List, Optional, Tuple

# RANSAC iterations per boundary. Fixed and modest: a chapter-sized candidate
# pool is at most a few thousand anchors, and the fit only needs to find *a*
# clean two-point consensus line, not to converge with statistical certainty.
_RANSAC_ITERATIONS = 64

# Fixed seed for the per-boundary `random.Random` instance. Determinism is a
# hard requirement (see docs/PLAN_OUT_OF_ORDER_NARRATION.md, Phase 0
# acceptance): identical input must produce byte-identical output across runs,
# or the map_quality regression veto (`is_regression`) compares a map against
# its own re-fit noise instead of a real change. 426 is this issue's number,
# chosen only for it to be recognizable, not for any statistical property.
_RANSAC_SEED = 426

# Base inlier tolerance, in seconds. This is a chapter-level consensus filter,
# not a word-level precision target (that precision comes later from CTC): it
# only needs to separate anchors that truly belong to this boundary's line
# from contamination (anchors that leaked in from a different, wrongly
# assumed, section) whose true error is on the order of minutes to hours, not
# seconds. 15s comfortably covers ordinary narration-pace drift within one
# section without being anywhere near that contamination scale.
_RANSAC_TOLERANCE_SECONDS = 15.0

# Proportional tolerance floor: a fraction of the candidate line's own
# predicted time span across the boundary (`char_end - char_start` chars at
# that line's slope). A short segment (a couple of pages) and a long one (a
# multi-hour novella) cannot share one absolute tolerance — 15s is generous
# for the former and far too tight for the latter, where ordinary pace
# variation alone can exceed it. Scaling the floor by the segment's own span
# keeps the *relative* tolerance roughly constant instead.
_RANSAC_TOLERANCE_FRACTION = 0.02

# Minimum inlier count for a segment fit to be accepted. Below this, "the
# line" is not statistically distinguishable from a handful of coincidental
# n-gram matches.
_MIN_SEGMENT_INLIERS = 8

# Minimum inlier fraction (of this boundary's own candidate anchors) for
# acceptance. A fit that only explains half of its own candidates is not a
# trustworthy placement even when the raw inlier count clears
# `_MIN_SEGMENT_INLIERS`.
_MIN_INLIER_FRACTION = 0.5

# Sane band for implied narration speed (ebook chars per second of audio).
# Outside this band the "fit" is almost certainly locking onto noise —
# scattered anchors that happen to fall inside this boundary's char range but
# do not actually belong to it — rather than real narration. Real books
# measured for this plan sit around 13-16 chars/sec; the band is deliberately
# wide around that to tolerate genuine pace variation (dialogue vs.
# description, fast vs. slow narrators).
_MIN_CHARS_PER_SEC = 2.0
_MAX_CHARS_PER_SEC = 60.0

# Above this fraction of the shorter segment's own duration, an overlap
# stops being ordinary seam bleed and becomes a genuine collision (one
# segment substantially claiming another's audio).
#
# Calibrated against real seam bleed measured on Tress of the Emerald Sea
# (`tests/fixtures/tress_seam_anchors.json`; issue #426) by running
# `fit_segments` over the book's real 81 non-empty spine boundaries with
# real candidate anchors - not synthetic data. Every dropped/winner pair at
# a true seam:
#
#   dropped chars   1604-  6179 (278.7s) vs winner  610678-615320 (259.2s): overlap  8.6s = 3.32%
#   dropped chars  94908- 97344 (158.5s) vs winner   97345-106088 (617.1s): overlap 10.0s = 6.31%  <- largest
#   dropped chars 173221-176660 (269.4s) vs winner  163584-173220 (710.5s): overlap  8.7s = 3.23%
#   dropped chars 541259-544753 (243.1s) vs winner  531973-541258 (690.3s): overlap  6.3s = 2.59%
#
# The largest real bleed fraction is 6.31%. This threshold sits more than
# double that with real margin, so ordinary least-squares seam noise
# (hundreds-of-seconds segments bleeding a few seconds at their shared edge)
# is never misclassified as a collision, while staying far below the
# near-total overlap a genuine collision produces (see
# `test_stronger_fit_survives_and_invariant_holds`, a 100%-overlap case).
# The synthetic 7ms bleed this module was originally calibrated against is
# not representative of real data and must not be used to set this value.
_SEAM_BLEED_MAX_OVERLAP_FRACTION = 0.15


@dataclass(frozen=True)
class Segment:
    """One ebook char range successfully fit to an audio timestamp range."""
    char_start: int
    char_end: int      # half-open
    ts_start: float
    ts_end: float
    inliers: int
    residual: float    # mean absolute residual of inliers, seconds


def _anchor_char(anchor: Dict) -> int:
    """Resolve a candidate anchor's char offset, preferring ``global_char``
    over ``char`` (mirrors ``map_quality.point_char``)."""
    if 'global_char' in anchor:
        return int(anchor['global_char'])
    return int(anchor.get('char', 0))


def _anchor_ts(anchor: Dict) -> float:
    """Resolve a candidate anchor's audio timestamp."""
    return float(anchor.get('ts', 0.0))


def _boundary_points(anchors: List[Dict], char_start: int, char_end: int) -> List[Tuple[int, float]]:
    """Candidate ``(char, ts)`` pairs for anchors whose char falls inside the
    half-open ``[char_start, char_end)`` boundary."""
    return [(_anchor_char(anchor), _anchor_ts(anchor)) for anchor in anchors
            if char_start <= _anchor_char(anchor) < char_end]


def _line_through(char1: int, ts1: float, char2: int, ts2: float) -> Tuple[float, float]:
    """Fit ``ts = a*char + b`` through two points with distinct char values.
    Callers are responsible for guaranteeing ``char1 != char2``."""
    a = (ts2 - ts1) / (char2 - char1)
    b = ts1 - a * char1
    return a, b


def _least_squares_fit(points: List[Tuple[int, float]]) -> Optional[Tuple[float, float]]:
    """Ordinary least-squares fit of ``ts = a*char + b`` over ``points``.

    Returns ``None`` when every point shares the same char (zero char
    variance, no slope is defined) — a defensive guard for a case the current
    caller cannot actually reach, since the winning RANSAC line's own sample
    pair (necessarily two distinct chars, residual zero against itself) is
    always part of its own inlier set.
    """
    n = len(points)
    mean_char = sum(char for char, _ in points) / n
    mean_ts = sum(ts for _, ts in points) / n
    numerator = sum((char - mean_char) * (ts - mean_ts) for char, ts in points)
    denominator = sum((char - mean_char) ** 2 for char, _ in points)
    if denominator == 0:
        return None
    a = numerator / denominator
    b = mean_ts - a * mean_char
    return a, b


def _inlier_tolerance(a: float, char_start: int, char_end: int) -> float:
    """Absolute residual tolerance for a candidate line over one boundary:
    the larger of the fixed base and a fraction of the line's own predicted
    span across the boundary (see `_RANSAC_TOLERANCE_FRACTION`).

    Scaling with the candidate line's own slope is safe even for an absurd
    sample pair (two anchors at nearly the same char with wildly different
    ts). Such a line earns a large tolerance, but its residuals grow far
    faster: the tolerance is `_RANSAC_TOLERANCE_FRACTION * |a| * span` while
    residuals across the boundary reach `|a| * span / 2` from a mid-boundary
    sample point — 25x larger at the current fraction. An implausible line
    therefore collects *fewer* inliers, not more, and can never win the
    consensus contest. Measured: 37 of 64 sample lines implausible, none ever
    becoming the best. The physically-implausible fits are rejected after the
    refit by the chars/sec band in `_fit_boundary`."""
    fitted_span_seconds = abs(a * (char_end - char_start))
    return max(_RANSAC_TOLERANCE_SECONDS, _RANSAC_TOLERANCE_FRACTION * fitted_span_seconds)


def _inliers_for_line(a: float, b: float, points: List[Tuple[int, float]],
                      tolerance: float) -> List[Tuple[int, float]]:
    """Points within ``tolerance`` seconds of the line ``ts = a*char + b``."""
    return [(char, ts) for char, ts in points if abs(a * char + b - ts) <= tolerance]


def _ransac_fit_boundary(points: List[Tuple[int, float]], char_start: int,
                         char_end: int) -> Optional[Tuple[float, float, List[Tuple[int, float]]]]:
    """RANSAC a line over one boundary's candidate ``(char, ts)`` points.

    Samples `_RANSAC_ITERATIONS` pairs, always with distinct char values (a
    vertical pair gives no slope), using a fresh `random.Random(_RANSAC_SEED)`
    per boundary so the result is deterministic and independent of any other
    boundary's fit. Returns ``(a, b, inlier_points)`` for the best consensus
    set, refit by least squares over its own inliers so the reported line is
    not just the two-point sample. ``None`` when fewer than two distinct char
    values exist, or when no consensus reaches two inliers.
    """
    char_groups: Dict[int, List[float]] = {}
    for char, ts in points:
        char_groups.setdefault(char, []).append(ts)
    distinct_chars = list(char_groups.keys())
    if len(distinct_chars) < 2:
        return None

    rng = random.Random(_RANSAC_SEED)
    best_inliers: List[Tuple[int, float]] = []
    for _ in range(_RANSAC_ITERATIONS):
        char1, char2 = rng.sample(distinct_chars, 2)
        ts1 = rng.choice(char_groups[char1])
        ts2 = rng.choice(char_groups[char2])
        a, b = _line_through(char1, ts1, char2, ts2)
        tolerance = _inlier_tolerance(a, char_start, char_end)
        inliers = _inliers_for_line(a, b, points, tolerance)
        if len(inliers) > len(best_inliers):
            best_inliers = inliers

    if len(best_inliers) < 2:
        return None
    refit = _least_squares_fit(best_inliers)
    if refit is None:
        return None
    a, b = refit
    return a, b, best_inliers


def _fit_boundary(anchors: List[Dict], char_start: int, char_end: int) -> Optional[Segment]:
    """Fit one ``(char_start, char_end)`` boundary to the audio timeline, or
    return ``None`` when it cannot be placed.

    A ``None`` result is a normal outcome, not an error: front matter, tables
    of contents and appendices legitimately have no matching audio.
    """
    points = _boundary_points(anchors, char_start, char_end)
    total = len(points)
    if total < _MIN_SEGMENT_INLIERS:
        return None

    fit = _ransac_fit_boundary(points, char_start, char_end)
    if fit is None:
        return None
    a, b, inlier_points = fit
    inlier_count = len(inlier_points)

    if inlier_count < _MIN_SEGMENT_INLIERS:
        return None
    if inlier_count / total < _MIN_INLIER_FRACTION:
        return None
    if a <= 0:
        return None
    chars_per_sec = 1.0 / a
    if not (_MIN_CHARS_PER_SEC <= chars_per_sec <= _MAX_CHARS_PER_SEC):
        return None

    residual = sum(abs(a * char + b - ts) for char, ts in inlier_points) / inlier_count
    ts_start = a * char_start + b
    ts_end = a * char_end + b
    return Segment(char_start=char_start, char_end=char_end, ts_start=ts_start,
                   ts_end=ts_end, inliers=inlier_count, residual=residual)


def _ts_overlap_seconds(first: Segment, second: Segment) -> float:
    """Seconds of shared audio between two segments (<= 0 when disjoint)."""
    return min(first.ts_end, second.ts_end) - max(first.ts_start, second.ts_start)


def _is_seam_bleed(first: Segment, second: Segment) -> bool:
    """True when the overlap between two overlapping segments is ordinary
    least-squares seam bleed rather than a genuine collision: small relative
    to the shorter of the two segments (see `_SEAM_BLEED_MAX_OVERLAP_FRACTION`
    for the real measurements this threshold is calibrated against).

    Callers are expected to have already established the two segments
    overlap at all (a positive `_ts_overlap_seconds`); a non-positive or
    degenerate overlap is defensively treated as not-bleed here.
    """
    overlap = _ts_overlap_seconds(first, second)
    if overlap <= 0:
        return False
    shortest = min(first.ts_end - first.ts_start, second.ts_end - second.ts_start)
    if shortest <= 0:
        return False
    return overlap <= _SEAM_BLEED_MAX_OVERLAP_FRACTION * shortest


def _trim_to_seam_midpoint(first: Segment, second: Segment) -> Optional[Tuple[Segment, Segment]]:
    """Resolve seam bleed between two overlapping segments by trimming both
    to the midpoint of their disputed span: the later segment's `ts_start`
    and the earlier segment's `ts_end` both become the midpoint of
    `[earlier.ts_end, later.ts_start]`. Both segments survive; disjointness
    is restored by construction.

    Accepts the two segments in either order and determines which is
    earlier/later by `ts_start`. Returns `None` - defer to genuine-collision
    handling instead - when trimming would invert or zero out either
    segment; that is not something ordinary seam bleed should ever produce.
    """
    earlier, later = (first, second) if first.ts_start <= second.ts_start else (second, first)
    midpoint = (earlier.ts_end + later.ts_start) / 2.0
    if midpoint <= earlier.ts_start or midpoint >= later.ts_end:
        return None
    return replace(earlier, ts_end=midpoint), replace(later, ts_start=midpoint)


def _is_stronger(candidate: Segment, incumbent: Segment) -> bool:
    """True when `candidate` should survive a conflict against `incumbent`:
    more inliers wins outright; a tie on inliers breaks on lower mean
    residual; a tie on both leaves `incumbent` in place."""
    if candidate.inliers != incumbent.inliers:
        return candidate.inliers > incumbent.inliers
    return candidate.residual < incumbent.residual


def _resolve_conflicts(segments: List[Segment]) -> List[Segment]:
    """Resolve overlapping-audio conflicts among placed segments.

    Processes segments in ``ts_start`` order (ties broken, in order, by more
    inliers then lower residual then lower ``char_start``, purely for
    determinism). Each candidate is compared against the last currently kept
    segment, and *any* positive overlap (`_ts_overlap_seconds` > 0) is
    examined - exact touch is fine (consecutive sections of a correctly
    narrated book abut in time) but every real overlap must be resolved, not
    just ones that clear some "is this material" threshold. Two kinds of
    resolution apply, in order:

    1. **Seam bleed** (`_is_seam_bleed`): the overlap is small relative to
       the shorter segment - ordinary least-squares error at a seam between
       two consecutive, correctly-narrated sections, not real competition
       for the same audio. Both segments are trimmed to the midpoint of the
       disputed span (`_trim_to_seam_midpoint`) and both survive.
    2. **Genuine collision**: the overlap is large relative to the shorter
       segment (or trimming would invert/zero one of them), meaning one
       segment is substantially claiming another's audio. The weaker of the
       two is dropped (see `_is_stronger`). A candidate that wins keeps
       checking against whatever it now sits next to — a cascading pop —
       since demoting one neighbour can expose another.

    Only ``kept[-1]`` is ever compared against ``current`` - `kept[-2]` and
    earlier are never rechecked here. That is still sound: `ordered` is
    sorted by each segment's *original* `ts_start`, so whichever of the two
    segments a trim classifies as "earlier" always has `ts_start` less than
    or equal to the other's - a trim never changes the earlier segment's
    `ts_start` (only its `ts_end` shrinks), and never changes which segment
    the earlier/later roles fall to, because a trim always leaves the pair
    touching exactly and the loop exits immediately after, before either
    segment could be trimmed a second time in this pass. So `kept[-1]`'s
    `ts_start` is invariant under trimming, and the disjointness already
    established against `kept[-2]` (when `kept[-1]` was appended) survives.
    A pop only ever falls back to an earlier, already-disjoint element for
    the same reason. Trimming therefore cannot introduce a new conflict with
    any other kept segment; only the pair directly in dispute needs
    rechecking, which the `while` loop does - this holds for a chain of any
    length, one seam at a time.
    """
    ordered = sorted(segments, key=lambda seg: (seg.ts_start, -seg.inliers, seg.residual, seg.char_start))
    kept: List[Segment] = []
    for segment in ordered:
        current: Optional[Segment] = segment
        while current is not None and kept and _ts_overlap_seconds(kept[-1], current) > 0:
            if _is_seam_bleed(kept[-1], current):
                trimmed = _trim_to_seam_midpoint(kept[-1], current)
                if trimmed is not None:
                    kept[-1], current = trimmed
                    continue
            if _is_stronger(current, kept[-1]):
                kept.pop()
            else:
                current = None
        if current is not None:
            kept.append(current)
    return kept


def _assert_disjoint(segments: List[Segment]) -> None:
    """Verify the invariant every downstream consumer relies on: placed
    segments are pairwise disjoint in char AND truly disjoint in ts (exact
    touch allowed, no tolerance - `_resolve_conflicts` resolves every
    positive overlap, so nothing less strict is honest here). A violation
    here is a bug in this module, not a normal data outcome."""
    for i in range(len(segments)):
        for j in range(i + 1, len(segments)):
            first, second = segments[i], segments[j]
            assert first.char_end <= second.char_start or second.char_end <= first.char_start, \
                f"char overlap between segments {first} and {second}"
            assert first.ts_end <= second.ts_start or second.ts_end <= first.ts_start, \
                f"ts overlap between segments {first} and {second}"


def fit_segments(anchors: List[Dict], boundaries: List[Tuple[int, int]],
                 total_chars: int) -> List[Segment]:
    """Fit each ``(char_start, char_end)`` boundary to the audio timeline
    independently, then resolve any placements that claim overlapping audio.

    For each boundary: take the candidate anchors whose char falls inside it,
    RANSAC a line ``ts = a*char + b`` over them (see `_ransac_fit_boundary`),
    and reject the boundary (leave it unplaced) when the fit is too weak, the
    slope is non-positive, or the implied chars/sec is outside a sane band
    (see `_fit_boundary`). Rejection is a normal outcome for unnarrated
    sections, never an error.

    Placed segments are then ordered by ``ts_start`` and any pair that still
    claims overlapping audio is resolved by keeping the stronger fit (see
    `_resolve_conflicts`). The returned list is sorted by ``ts_start`` and is
    guaranteed pairwise disjoint in both char and ts (asserted).

    Returns ``[]`` for empty ``anchors``, empty ``boundaries``, a non-positive
    ``total_chars``, or when no boundary places — never raises for bad input.
    A boundary outside ``[0, total_chars]`` or with ``char_end <= char_start``
    is silently skipped as malformed rather than fit.
    """
    if not anchors or not boundaries or total_chars <= 0:
        return []

    placed: List[Segment] = []
    for char_start, char_end in boundaries:
        if char_start < 0 or char_end <= char_start or char_end > total_chars:
            continue
        segment = _fit_boundary(anchors, char_start, char_end)
        if segment is not None:
            placed.append(segment)

    resolved = _resolve_conflicts(placed)
    resolved.sort(key=lambda seg: seg.ts_start)
    _assert_disjoint(resolved)
    return resolved
