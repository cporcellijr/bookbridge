"""
Map Quality scoring (issue #426).

Pure functions that score an alignment map's positional reliability. No DB access,
no I/O, and no imports from ``src.db`` or ``src.services.alignment_service`` — this
module exists so a caller (e.g. a CTC second-pass remap decision) can veto a
challenger map that regresses against the incumbent, without depending on
AlignmentService at all.

An alignment map is a list of ``{"char": int, "ts": float}`` points sorted by char;
some legacy maps carry ``global_char`` instead of ``char`` (see `point_char`).
"""

import bisect
import json
import math
import re
from dataclasses import dataclass
from statistics import median
from typing import Dict, List, Optional, Tuple

# The interpolated-gap reference deliberately matches
# AlignmentService._CTC_MAX_GAP_FRACTION (0.25): a map whose worst gap already
# fails that acceptance gate should also score near zero on this axis.
_GAP_REFERENCE_FRACTION = 0.25

# Density-spread band: max(max(rate)/median(rate), median(rate)/min(rate)) of
# per-slice chars-per-second across `_DENSITY_SLICE_COUNT` equal-ANCHOR-COUNT
# slices (see `_density_spread`). <=2x reads as evenly-paced narration; >=10x is
# the profile measured on the real Four Past Midnight CTC map (one anchor-decile
# crawling at ~3 c/s, another racing at ~59 c/s against a 12-22 c/s baseline) —
# a near-linear interpolation run hiding behind a passable gap fraction and
# anchor count. Left uncalibrated pending validation against the live map corpus;
# do not retune without that data.
_DENSITY_SPREAD_GOOD = 2.0
_DENSITY_SPREAD_BAD = 10.0

# Anchor density (anchors per 1000 covered chars) at which the anchor sub-score
# saturates to 1.0.
_ANCHOR_DENSITY_REFERENCE = 10.0

# Weighted-sum weights for `score`. Density spread carries the most weight because
# it is the metric that catches an uneven interpolation run that a passable gap
# fraction and anchor count alone can hide.
_WEIGHT_GAP = 0.35
_WEIGHT_DENSITY = 0.45
_WEIGHT_ANCHOR = 0.10
_WEIGHT_ORDER = 0.10

_DENSITY_SLICE_COUNT = 20
_MIN_VALID_DENSITY_SLICES = 4

# Default margin for `is_regression`: a challenger must score more than this far
# below the incumbent to be vetoed, so a book does not churn on measurement noise
# between two near-equivalent maps across repeated re-align cycles.
DEFAULT_REGRESSION_MARGIN = 0.02

# Below this, a stored map is flagged for LLM re-alignment regardless of its
# `align_method` (issue #426 phase 4: a 'lexical' map can still be badly broken —
# Immortal Mana, Starfish, Bestial, and Four Past Midnight all carry a 'lexical'
# map yet are genuinely defective). Calibrated against a live measurement across
# 383 stored maps: 360 score >= 0.90, and every genuinely defective map scores
# <= 0.71, so 0.75 sits in the gap and separates them cleanly.
ALIGNMENT_QUALITY_REALIGN_THRESHOLD = 0.75


@dataclass(frozen=True)
class MapQuality:
    """Aggregate positional-reliability metrics for one alignment map."""
    anchors: int
    span_chars: int
    max_gap_fraction: float
    anchor_density: float       # anchors per 1000 covered chars
    density_spread: float       # max(max/median, median/min) of per-slice chars-per-second
                                 # across equal-anchor-count slices; inf when unmeasurable
    backwards_fraction: float   # fraction of adjacent anchor pairs whose ts decreases
    score: float                # 0.0-1.0, higher is better


def quality_detail_json(quality: MapQuality) -> str:
    """Serialize a `MapQuality`'s fields to a JSON object, for storage in
    `BookAlignment.quality_detail`.

    `density_spread` (and, in principle, the other ratio/fraction fields) is
    legitimately `float('inf')` for a degenerate map (see `_density_spread`);
    `json.dumps` emits the bare token ``Infinity`` for it, which is not valid
    JSON and will not round-trip through `json.loads`. Every field is passed
    through `math.isfinite` and stored as `null` when it isn't finite.
    """
    def _finite_or_none(value: float) -> Optional[float]:
        return value if math.isfinite(value) else None

    detail = {
        "anchors": quality.anchors,
        "span_chars": quality.span_chars,
        "max_gap_fraction": _finite_or_none(quality.max_gap_fraction),
        "anchor_density": _finite_or_none(quality.anchor_density),
        "density_spread": _finite_or_none(quality.density_spread),
        "backwards_fraction": _finite_or_none(quality.backwards_fraction),
    }
    return json.dumps(detail)


def point_char(point: Dict) -> int:
    """Resolve an alignment point's char offset, preferring ``global_char`` over
    ``char`` (mirrors ``AlignmentService._point_char``)."""
    if 'global_char' in point:
        return int(point['global_char'])
    return int(point.get('char', 0))


def max_gap_fraction(alignment_map: Optional[List[Dict]],
                      exclude_spans: Optional[List[Tuple[int, int]]] = None) -> float:
    """Largest char span between consecutive anchors, as a fraction of the map's
    covered range, with intentional exclusions removed from both. Returns
    1.0 for a map too small to judge (degenerate)."""
    if not alignment_map or len(alignment_map) < 2:
        return 1.0
    chars = sorted(int(point.get("char", point.get("global_char", 0)))
                   for point in alignment_map)
    # Collapse the union of exclusions into a narrated-only coordinate space.
    # A single sweep avoids multiplying dense per-word anchors by span count.
    spans = sorted((lo, hi) for lo, hi in (exclude_spans or []) if lo < hi)
    if spans:
        adjusted = []
        removed, cursor, span_idx = 0, chars[0], 0
        for char in chars:
            while span_idx < len(spans) and spans[span_idx][0] < char:
                lo, hi = spans[span_idx]
                removed += max(0, min(char, hi) - max(cursor, lo))
                cursor = max(cursor, min(char, hi))
                if hi > char:
                    break
                span_idx += 1
            adjusted.append(char - removed)
        chars = adjusted
    span = chars[-1] - chars[0]
    if span <= 0:
        return 1.0
    max_gap = max(chars[i + 1] - chars[i] for i in range(len(chars) - 1))
    return max_gap / span


def _collapse_excluded(chars: List[int], exclude_spans: Optional[List[Tuple[int, int]]]) -> List[int]:
    """Collapse the union of exclusions out of an already char-sorted list, in the
    same single left-to-right sweep `max_gap_fraction` uses. Used by the metrics
    below (which resolve points via `point_char`, unlike `max_gap_fraction`'s own
    inline resolution)."""
    spans = sorted((lo, hi) for lo, hi in (exclude_spans or []) if lo < hi)
    if not spans or not chars:
        return list(chars)
    adjusted = []
    removed, cursor, span_idx = 0, chars[0], 0
    for char in chars:
        while span_idx < len(spans) and spans[span_idx][0] < char:
            lo, hi = spans[span_idx]
            removed += max(0, min(char, hi) - max(cursor, lo))
            cursor = max(cursor, min(char, hi))
            if hi > char:
                break
            span_idx += 1
        adjusted.append(char - removed)
    return adjusted


def _density_spread(collapsed_points: List[Tuple[int, float]]) -> float:
    """Median-relative spread of per-slice chars-per-second across
    `_DENSITY_SLICE_COUNT` equal-ANCHOR-COUNT slices of the (already
    exclusion-collapsed) map — ``points[k * n // COUNT : (k + 1) * n // COUNT]``.

    Slicing by anchor count rather than char width is what lets a single
    anomalous run of anchors stand out: the real pathology (see Four Past
    Midnight) is a small fraction of the anchors covering a hugely
    disproportionate share of the chars (or vice versa) — equal-char-width
    slicing dilutes that run across many slices and averages it away.

    A median-relative ratio (rather than a percentile ratio) is used for the
    same reason: with as few as 20 slices, percentile trimming discards the
    one or two most extreme slices at each end — exactly where this pathology
    lives — while the median stays robust to a single anomalous slice.

    Returns inf when fewer than `_MIN_VALID_DENSITY_SLICES` slices yield a
    usable rate, or when the median or minimum rate is non-positive.
    """
    n = len(collapsed_points)
    if n < 2:
        return float('inf')
    rates: List[float] = []
    for k in range(_DENSITY_SLICE_COUNT):
        chunk = collapsed_points[k * n // _DENSITY_SLICE_COUNT:(k + 1) * n // _DENSITY_SLICE_COUNT]
        if len(chunk) < 2:
            continue
        first_char, first_ts = chunk[0]
        last_char, last_ts = chunk[-1]
        ts_delta = last_ts - first_ts
        if ts_delta <= 0:
            continue
        rates.append((last_char - first_char) / ts_delta)
    if len(rates) < _MIN_VALID_DENSITY_SLICES:
        return float('inf')
    median_rate = median(rates)
    if median_rate <= 0 or min(rates) <= 0:
        return float('inf')
    return max(max(rates) / median_rate, median_rate / min(rates))


def _clamp(value: float) -> float:
    """Clamp a sub-score to [0.0, 1.0]."""
    return max(0.0, min(1.0, value))


def _segment_bounds(segment) -> Tuple[int, int]:
    """``(char_start, char_end)`` from one segment, accepting either a plain
    dict (as stored in ``segments_json`` / returned by
    ``AlignmentService._get_segments``) or an attribute-bearing object such
    as ``segment_fit.Segment`` (a challenger's in-memory placements, passed
    straight through by ``AlignmentService._publish_map``). Callers should
    never have to convert one shape into the other just to score -- this
    module stays independent of `segment_fit` (see the module docstring),
    so it duck-types instead of importing `Segment` for an isinstance check.
    """
    if isinstance(segment, dict):
        return int(segment['char_start']), int(segment['char_end'])
    return int(segment.char_start), int(segment.char_end)


def _segment_slice_points(sorted_points: List[Tuple[int, float]],
                          char_start: int, char_end: int) -> List[Tuple[int, float]]:
    """The contiguous slice of ``sorted_points`` (sorted by char, original --
    pre-exclusion-collapse -- coordinates) whose char falls in the half-open
    ``[char_start, char_end)`` range. Bisects on the ``(char, ts)`` tuples
    directly: pairing each bound with ``-inf`` as the ts half of the search
    key lands `bisect_left` at the first real point whose char is >= the
    bound, regardless of what ts that point carries."""
    lo = bisect.bisect_left(sorted_points, (char_start, float('-inf')))
    hi = bisect.bisect_left(sorted_points, (char_end, float('-inf')))
    return sorted_points[lo:hi]


def _segment_aware_density_spread(sorted_points: List[Tuple[int, float]], segments: List,
                                  exclude_spans: Optional[List[Tuple[int, int]]]) -> float:
    """Segment-aware `density_spread` (issue #426 phase 3): run `_density_spread`
    independently over each segment's own char slice of ``sorted_points`` --
    resolved in original, pre-exclusion-collapse char coordinates, since that
    is the space segment boundaries are always defined in -- then aggregate
    the per-segment values with `max`, so one badly-paced segment still drags
    the score down even when every other segment is excellent.

    This is not the same axis whole-map `_density_spread` measures. Segments
    can (and correctly do) abut or jump discontinuously in `ts` at their own
    seams -- that is real audio structure for a book whose narration order
    differs from its spine order, not measurement error -- and scoring that
    discontinuity as part of one undivided run inflates the whole-map figure
    by a factor that has nothing to do with how evenly-paced the narration
    actually is. Measured on the real Four Past Midnight segmented map:
    whole-map `density_spread` 5.394 vs a max-of-per-segment 1.327 (median
    1.146 across the 12 placed segments) -- the whole-map number is ~4x
    inflated purely by seam discontinuities that a segmented map's own metric
    must not punish.

    A segment with too few points to measure (`_density_spread` returns
    `inf`) is skipped rather than folded into the aggregate as "badly paced"
    -- `inf` means unmeasurable, not poorly paced, and a short trailing
    segment should not by itself zero out an otherwise excellent score. If
    every segment is unmeasurable this still returns `inf`, matching the
    whole-map degenerate case in `_density_spread` itself.
    """
    spreads = []
    for segment in segments:
        char_start, char_end = _segment_bounds(segment)
        points = _segment_slice_points(sorted_points, char_start, char_end)
        if len(points) < 2:
            continue
        chars = _collapse_excluded([char for char, _ in points], exclude_spans)
        segment_collapsed = list(zip(chars, (ts for _, ts in points)))
        spread = _density_spread(segment_collapsed)
        if math.isfinite(spread):
            spreads.append(spread)
    if not spreads:
        return float('inf')
    return max(spreads)


def score_map(alignment_map: Optional[List[Dict]],
              exclude_spans: Optional[List[Tuple[int, int]]] = None,
              segments: Optional[List] = None) -> MapQuality:
    """Score an alignment map's positional reliability on several independent
    axes, then combine them into a single 0.0-1.0 `score` (higher is better).

    Safe on degenerate input: ``None``, an empty list, and a single-point map all
    return a `MapQuality` with `score` 0.0 and `max_gap_fraction` 1.0 rather than
    raising.

    `segments` (issue #426 phase 3), when supplied non-empty, makes
    `density_spread` segment-aware: computed independently over each
    segment's own char slice and aggregated with `max` (see
    `_segment_aware_density_spread`) instead of over the whole map in one
    run. Each entry may be a plain dict with `char_start`/`char_end` keys or
    any object exposing those as attributes (`segment_fit.Segment`) --
    callers pass whichever shape they already have.

    `max_gap_fraction` deliberately stays whole-map even when `segments` is
    supplied -- placed segments tile the char space contiguously, so a char
    gap is a real gap regardless of which segment it falls in. Measured on
    Four Past Midnight: per-segment gap fractions (0.002-0.02) agree with the
    whole-map figure (0.0221); making this metric segment-aware would be
    churn with no behavioral difference.

    `segments=None` (or an empty list) reproduces today's whole-map score
    exactly -- this is the compatibility guarantee for the 372 already-stored
    maps that predate segmentation and carry no segment index at all.
    """
    if not alignment_map:
        return MapQuality(anchors=0, span_chars=0, max_gap_fraction=1.0,
                           anchor_density=0.0, density_spread=float('inf'),
                           backwards_fraction=0.0, score=0.0)

    anchors = len(alignment_map)
    gap_fraction = max_gap_fraction(alignment_map, exclude_spans)

    if anchors < 2:
        return MapQuality(anchors=anchors, span_chars=0, max_gap_fraction=gap_fraction,
                           anchor_density=0.0, density_spread=float('inf'),
                           backwards_fraction=0.0, score=0.0)

    sorted_points = sorted(((point_char(point), float(point.get('ts', 0.0)))
                            for point in alignment_map), key=lambda pair: pair[0])
    chars = _collapse_excluded([char for char, _ in sorted_points], exclude_spans)
    collapsed_points = list(zip(chars, (ts for _, ts in sorted_points)))

    span_chars = chars[-1] - chars[0]
    anchor_density = 1000.0 * anchors / span_chars if span_chars > 0 else 0.0
    spread = (_segment_aware_density_spread(sorted_points, segments, exclude_spans)
              if segments else _density_spread(collapsed_points))

    backwards = sum(1 for i in range(len(sorted_points) - 1)
                    if sorted_points[i + 1][1] < sorted_points[i][1])
    backwards_fraction = backwards / (anchors - 1)

    gap_score = _clamp(1.0 - gap_fraction / _GAP_REFERENCE_FRACTION)
    density_score = _clamp((_DENSITY_SPREAD_BAD - spread) / (_DENSITY_SPREAD_BAD - _DENSITY_SPREAD_GOOD))
    anchor_score = _clamp(anchor_density / _ANCHOR_DENSITY_REFERENCE)
    order_score = _clamp(1.0 - backwards_fraction)

    score = (_WEIGHT_GAP * gap_score + _WEIGHT_DENSITY * density_score +
             _WEIGHT_ANCHOR * anchor_score + _WEIGHT_ORDER * order_score)

    return MapQuality(anchors=anchors, span_chars=span_chars, max_gap_fraction=gap_fraction,
                       anchor_density=anchor_density, density_spread=spread,
                       backwards_fraction=backwards_fraction, score=score)


def is_regression(incumbent: MapQuality, challenger: MapQuality,
                   margin: float = DEFAULT_REGRESSION_MARGIN) -> bool:
    """Return True when `challenger` is a material regression against `incumbent`.

    This is deliberately a regression veto, not a "challenger must win" test.
    Two maps of very different real positional precision — e.g. a healthy CTC
    map and a healthy lexical map — score near-identically here: both look
    equally perfect on the gap, density-spread and anchor-count axes, because
    those axes can't see word-level precision, only gross structural health.
    Live data confirms this: the great majority of real maps score >= 0.90, and
    every healthy CTC-vs-lexical pair ties within a hundredth. Requiring the
    challenger to score higher by any margin would therefore reject nearly
    every healthy CTC replacement and silently disable the CTC upgrade path.
    So the score is only trustworthy as a detector of material degradation —
    a new map that is measurably worse — never as a tie-breaker between two
    maps that both look healthy.
    """
    return challenger.score < incumbent.score - margin


# Minimum size, as a fraction of the ebook's total chars, for a run of anchors
# `_filter_monotonic_lis` discarded to be reported by `detect_out_of_order_blocks`
# as a structural EPUB/audio mismatch. Below this a discarded run is ordinary
# matcher noise — a handful of scattered false anchors, a repeated phrase caught
# twice — not a misordered chapter/section.
_OUT_OF_ORDER_MIN_BLOCK_FRACTION = 0.05

# Minimum anchor count for the same purpose, alongside the fraction above: a run
# spanning many chars on only a couple of widely-spaced anchors is exactly the
# kind of sparse noise the fraction threshold alone would let through.
_OUT_OF_ORDER_MIN_BLOCK_ANCHORS = 50

# A run of discarded anchors can clear both thresholds above and still not mean
# "this text was relocated" — it can mean the retained map already covers that
# text densely and the discarded run is just scattered duplicate/spurious
# matches (common in a short-story collection with repeated phrasing). Live
# measurement on "The Ladies of Grace Adieu and Other Stories" (42,716
# candidates, 8,211 dropped, 4 blocks passing the thresholds above): the
# char-58,919-154,526 block (26.1% of the book) contained 7,742 kept anchors
# with a largest internal gap of only 1,551 chars; char-158,524-251,259 (25.3%)
# contained 11,060 kept anchors, largest gap 341 chars; char-251,430-313,523
# (16.9%) contained 6,972 kept anchors, largest gap 280 chars — all three
# false positives over thoroughly-anchored text. Only the fourth block
# (char-328,265-364,943, 10.0%) was genuine, with zero kept anchors inside it
# (gap = the whole 36,678-char block). By contrast, on Four Past Midnight (the
# real permutation this detector was built for) the LIS drops whole novellas,
# so the reported blocks contain zero kept anchors. A block therefore also
# needs its own char range to be substantially uncovered by `kept` — the
# largest gap left uncovered within it (see `_largest_uncovered_gap`) must be
# at least this fraction of the block's own char width — before it is
# reported.
_OUT_OF_ORDER_MIN_UNCOVERED_FRACTION = 0.5


def _largest_uncovered_gap(char_start: int, char_end: int, kept_chars: List[int]) -> int:
    """Largest gap left uncovered by ``kept_chars`` (sorted) within
    ``[char_start, char_end]``.

    The range's own edges bound coverage the same way a kept anchor would --
    coverage cannot extend past them -- so the gap sequence runs over
    ``char_start``, every entry of ``kept_chars`` that falls inside the range,
    and ``char_end``. This is what lets a block covered densely on only part
    of its range (e.g. the first half) register the uncovered remainder as a
    gap, not just the space between two covered anchors. Fewer than 2 entries
    inside the range skips straight to fully uncovered (gap = the whole
    ``char_end - char_start`` width) rather than the two edge-to-point gaps a
    single anchor would otherwise produce.
    """
    block_width = char_end - char_start
    lo = bisect.bisect_left(kept_chars, char_start)
    hi = bisect.bisect_right(kept_chars, char_end)
    inside = kept_chars[lo:hi]
    if len(inside) < 2:
        return block_width
    boundary_points = [char_start] + inside + [char_end]
    return max(b - a for a, b in zip(boundary_points, boundary_points[1:]))


def detect_out_of_order_blocks(anchors: List[Dict], kept: List[Dict],
                               total_chars: int) -> List[Dict]:
    """Detect large runs of anchors that `AlignmentService._filter_monotonic_lis`
    discarded because they could not chain onto the retained (longest strictly
    increasing) subsequence — the signature of a book whose EPUB spine order
    does not match its audiobook's narration order (issue #426: Four Past
    Midnight, a four-novella collection narrated in published order 1-2-3-4 but
    spined 2-4-3-1, where the LIS can keep anchors from at most two of the four
    blocks).

    ``anchors`` is every candidate anchor sorted by ``char``; ``kept`` is the
    subsequence `_filter_monotonic_lis` retained. The two are compared by
    object identity (``id()``), not value, since duplicate ``{char, ts}`` pairs
    across anchors would otherwise collide.

    The discarded anchors (still in char order) are split into maximal runs
    whose ``ts`` is non-decreasing. A run is reported only when it clears both
    `_OUT_OF_ORDER_MIN_BLOCK_FRACTION` (of ``total_chars``) and
    `_OUT_OF_ORDER_MIN_BLOCK_ANCHORS`, *and* its own char range is left
    substantially uncovered by ``kept`` (see `_OUT_OF_ORDER_MIN_UNCOVERED_FRACTION`)
    — a run of discarded anchors over text the retained map already covers
    densely is duplicate/spurious matching, not a relocated section.

    Returns a list of ``{"char_start", "char_end", "ts_start", "ts_end",
    "anchors"}`` dicts, sorted by descending char span (``char_end -
    char_start``). Returns ``[]`` for empty/degenerate input, when
    ``total_chars`` <= 0, or when no run qualifies. Never raises.
    """
    if not anchors or total_chars <= 0:
        return []

    kept_ids = {id(anchor) for anchor in kept}
    discarded = [anchor for anchor in anchors if id(anchor) not in kept_ids]
    if not discarded:
        return []

    runs: List[List[Dict]] = []
    for anchor in discarded:
        if runs and anchor['ts'] >= runs[-1][-1]['ts']:
            runs[-1].append(anchor)
        else:
            runs.append([anchor])

    min_span = _OUT_OF_ORDER_MIN_BLOCK_FRACTION * total_chars
    kept_chars = sorted(point_char(anchor) for anchor in kept)
    blocks = []
    for run in runs:
        char_start, char_end = run[0]['char'], run[-1]['char']
        if (char_end - char_start) < min_span or len(run) < _OUT_OF_ORDER_MIN_BLOCK_ANCHORS:
            continue
        block_width = char_end - char_start
        largest_gap = _largest_uncovered_gap(char_start, char_end, kept_chars)
        if largest_gap < _OUT_OF_ORDER_MIN_UNCOVERED_FRACTION * block_width:
            continue
        blocks.append({
            "char_start": char_start,
            "char_end": char_end,
            "ts_start": run[0]['ts'],
            "ts_end": run[-1]['ts'],
            "anchors": run,
        })

    blocks.sort(key=lambda block: block["char_end"] - block["char_start"], reverse=True)
    return blocks


# --------------------------------------------------------------------------- #
# Non-LLM content-match guard (issue #426)
# --------------------------------------------------------------------------- #
#
# `transcript_text_overlap` is the lexical fallback `AlignmentService._verify_content_match`
# uses when the embedding path (Ollama) is unavailable -- it is otherwise a permanent
# no-op on any install without Ollama, which let eight mismatched audio/ebook pairings
# on the live install get stored as maps that synced garbage positions silently.
#
# Tokenization: lowercase `[a-z0-9']+` word tokens.
#
# CALIBRATION -- measured by running THIS function (samples=200, ngram=6) over 28
# real book/transcript pairs on the live library. These are its actual return
# values, not a coarser probe's:
#
#   Prodigal Blues            0.850   |  State of Fear          0.645
#   Toplin                    0.825   |  Jade Legacy            0.630
#   Flowers for Algernon      0.805   |  American Elsewhere     0.610
#   The Ferryman              0.780   |  Outer Dark             0.580
#   Rose Madder               0.765   |  Neuromancer            0.570
#   Hollow Kingdom            0.730   |  Megalodon In Paradise  0.555
#   Four Past Midnight        0.710   |  Push (Unabridged)      0.300  <- floor
#
# Every one of those is a correct pairing, so 0.300 is the lowest legitimate value
# observed. Note the healthy range runs well below 1.0 even for a perfect pairing:
# ASR never reproduces the text verbatim, so most sampled 6-grams legitimately miss.
#
# The mismatched pairings this guard exists to catch scored 0-4% on the coarser
# probe used to diagnose them (Nosferatu Academy, Bad Man and Mob Sorcery Book 4 at
# literally zero matching probe points; Let the Old Dreams Die at 3.75%). They were
# deleted before this function existed, so they could not be re-measured with it --
# but with zero shared n-grams the value here is ~0.00 by construction.
#
# CONTENT_MATCH_MIN_OVERLAP therefore defaults to 0.15: half the lowest legitimate
# value measured (0.300) and several times the highest plausible mismatch (~0.04).
# Do not raise it toward the healthy floor without re-running that sweep -- at 0.25
# the margin under Push is only 0.05, one bad transcript away from a false refusal.

# Below this many tokens on either side, there are too few n-grams to sample
# meaningfully -- return 1.0 ("cannot judge, do not block") rather than a noisy score.
_MIN_TOKENS_FOR_OVERLAP = 50

_WORD_TOKEN_RE = re.compile(r"[a-z0-9']+")


def _word_tokens(text: str) -> List[str]:
    """Lowercase word tokens, matching the probe scripts behind the calibration
    table above."""
    return _WORD_TOKEN_RE.findall((text or "").lower())


def transcript_text_overlap(transcript_text: str, ebook_text: str,
                            samples: int = 200, ngram: int = 6) -> float:
    """Fraction of sampled ebook n-grams that also occur in the transcript.

    Builds a set of every `ngram`-token window in the transcript once, then
    samples up to `samples` `ngram`-token windows at evenly spaced token offsets
    across the *entire* ebook -- not just the opening, since front matter is
    often unnarrated and would skew a head-only sample -- and returns
    matched / sampled.

    Returns 1.0 (cannot judge, do not block) when either side has fewer than
    `_MIN_TOKENS_FOR_OVERLAP` tokens.
    """
    transcript_tokens = _word_tokens(transcript_text)
    ebook_tokens = _word_tokens(ebook_text)
    if (len(transcript_tokens) < _MIN_TOKENS_FOR_OVERLAP
            or len(ebook_tokens) < _MIN_TOKENS_FOR_OVERLAP
            or len(transcript_tokens) < ngram or len(ebook_tokens) < ngram):
        return 1.0

    transcript_ngrams = {
        tuple(transcript_tokens[i:i + ngram])
        for i in range(len(transcript_tokens) - ngram + 1)
    }

    max_offset = len(ebook_tokens) - ngram
    sample_count = min(samples, max_offset + 1)
    if sample_count <= 1:
        offsets = [0]
    else:
        offsets = [max_offset * i // (sample_count - 1) for i in range(sample_count)]

    matched = sum(
        1 for offset in offsets
        if tuple(ebook_tokens[offset:offset + ngram]) in transcript_ngrams
    )
    return matched / len(offsets)
