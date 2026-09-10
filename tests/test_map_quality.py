"""Issue #426: MapQuality scoring — pure functions with no DB/service dependency.

score_map's density_spread axis is the metric this phase adds: existing gap-fraction
and anchor-count checks can pass while an interpolated map still has a wildly uneven
chars/sec profile (e.g. a slow prologue vs a fast recap chapter, the real Four Past
Midnight profile that motivated this metric). density_spread slices by equal ANCHOR
COUNT (not equal char width) and uses a median-relative ratio (not a p90/p10
percentile ratio) — both corrections were made after validating against the live
map corpus; see the two dedicated tests below that fail against the earlier,
wrong implementations.
"""

import unittest
from typing import Dict, List

import math

from src.services.map_quality import (
    MapQuality,
    _SEGMENT_MIN_POINTS_FOR_DENSITY,
    is_regression,
    score_map,
)

_ANCHORS_PER_GROUP = 10


def _dense_map(span_chars: int, char_step: int, rate_chars_per_sec: float) -> List[Dict]:
    """An evenly-paced synthetic map: constant chars/sec throughout."""
    return [{"char": c, "ts": c / rate_chars_per_sec}
            for c in range(0, span_chars + 1, char_step)]


def _grouped_map(chars_per_group: List[int], rates: List[float]) -> List[Dict]:
    """Build a map of len(chars_per_group) * _ANCHORS_PER_GROUP anchors, split into
    equal-anchor-count groups (one per `chars_per_group`/`rates` entry). Group i
    covers chars_per_group[i] chars at rates[i] chars/sec, with anchors spaced
    evenly in char (and, within the group, in time) across the group."""
    points: List[Dict] = []
    char_cursor = 0.0
    ts_cursor = 0.0
    for chars, rate in zip(chars_per_group, rates):
        char_step = chars / _ANCHORS_PER_GROUP
        ts_step = char_step / rate
        for _ in range(_ANCHORS_PER_GROUP):
            char_cursor += char_step
            ts_cursor += ts_step
            points.append({"char": round(char_cursor), "ts": ts_cursor})
    return points


class TestScoreMapDegenerateInput(unittest.TestCase):

    def test_none_returns_zero_score_without_raising(self):
        quality = score_map(None)
        self.assertEqual(quality.score, 0.0)
        self.assertEqual(quality.max_gap_fraction, 1.0)

    def test_empty_list_returns_zero_score_without_raising(self):
        quality = score_map([])
        self.assertEqual(quality.score, 0.0)
        self.assertEqual(quality.max_gap_fraction, 1.0)

    def test_single_point_returns_zero_score_without_raising(self):
        quality = score_map([{"char": 10, "ts": 1.0}])
        self.assertEqual(quality.score, 0.0)
        self.assertEqual(quality.max_gap_fraction, 1.0)


class TestScoreMapQualitativeCases(unittest.TestCase):

    def test_dense_evenly_paced_map_scores_near_one(self):
        amap = _dense_map(100000, 100, 100.0)
        quality = score_map(amap)
        self.assertEqual(quality.anchors, 1001)
        self.assertLess(quality.max_gap_fraction, 0.01)
        self.assertGreater(quality.score, 0.95)

    def test_two_point_degenerate_map_scores_near_zero(self):
        amap = [{"char": 0, "ts": 0.0}, {"char": 100000, "ts": 1000.0}]
        quality = score_map(amap)
        self.assertEqual(quality.max_gap_fraction, 1.0)
        self.assertLess(quality.anchor_density, 1.0)
        self.assertLess(quality.score, 0.15)

    def test_huge_interpolated_gap_scores_materially_lower(self):
        base = _dense_map(100000, 100, 100.0)
        # Drop every interior anchor in a 40,000-char stretch — one huge
        # interpolated gap — while leaving the rest of the map untouched.
        gapped = [point for point in base if not (40000 < point["char"] < 80000)]

        before = score_map(base)
        after = score_map(gapped)

        self.assertAlmostEqual(after.max_gap_fraction, 0.4, places=3)
        self.assertLess(after.score, before.score - 0.25)


class TestDensitySpreadDiscriminates(unittest.TestCase):
    """The whole point of this phase: two maps with identical anchor count and
    identical max_gap_fraction must still be told apart by density_spread, and
    that difference must move the final score.

    Both maps below share the exact same (deliberately non-uniform) anchor char
    layout — a sparse first anchor-group spanning a disproportionately large
    char range, a dense last anchor-group spanning a disproportionately small
    one, baseline in between — modeled on the real Four Past Midnight anchor
    distribution ("the first 10% of anchors cover 124k chars but 38,000
    seconds"). Sharing the char layout is what makes anchor count and
    max_gap_fraction come out identical by construction: a genuinely
    evenly-char-spaced map is provably the unique minimum-gap-fraction layout
    for a given anchor count (max >= mean, equality only when uniform), so a
    map with any non-uniform layout can never tie a uniform one on
    max_gap_fraction. Only the per-group PACE (chars/sec) differs between the
    two maps: constant throughout the "even" map, anomalously slow/fast in two
    groups of the "uneven" one.
    """

    def test_even_pace_scores_higher_than_uneven_pace(self):
        # First group is anchor-sparse (50,000 chars), last is anchor-dense
        # (500 chars), 18 baseline groups make up the rest — same layout for
        # both maps, 20 groups x 10 anchors = 200 anchors, span 100,000 chars.
        chars_per_group = [50000] + [2750] * 18 + [500]
        self.assertEqual(sum(chars_per_group), 100000)

        even_rates = [15.0] * 20
        # Same shape as the real Four Past Midnight profile: one slow
        # anchor-decile (~3 c/s), one fast one (~59 c/s), ~15 c/s elsewhere.
        uneven_rates = [3.0] + [15.0] * 18 + [59.0]

        even_map = _grouped_map(chars_per_group, even_rates)
        uneven_map = _grouped_map(chars_per_group, uneven_rates)

        even_quality = score_map(even_map)
        uneven_quality = score_map(uneven_map)

        # Same char layout in both maps, so these two axes must be identical —
        # only density_spread (and therefore score) may differ.
        self.assertEqual(even_quality.anchors, uneven_quality.anchors)
        self.assertAlmostEqual(even_quality.max_gap_fraction, uneven_quality.max_gap_fraction, places=9)

        self.assertLess(even_quality.density_spread, uneven_quality.density_spread)
        self.assertGreater(even_quality.score, uneven_quality.score)

    def test_density_spread_detects_a_single_anomalous_slice(self):
        """A lone slow anchor-decile out of 20 must still move density_spread —
        this is exactly what p90/p10 percentile trimming missed (the metric's
        second wrong turn): with only one outlier, int(0.1*20)==2 and
        min(int(0.9*20),19)==18 both land on a normal neighbor, not the
        anomaly, because trimming discards the two most extreme slices at each
        end. A median-relative ratio stays robust to a single anomalous slice."""
        baseline_rate, slow_rate = 15.0, 3.0
        rates = [slow_rate] + [baseline_rate] * 19
        amap = _grouped_map([100] * 20, rates)

        quality = score_map(amap)

        self.assertAlmostEqual(quality.density_spread, baseline_rate / slow_rate, places=2)


class TestSegmentAwareDensitySpread(unittest.TestCase):
    """Issue #426 phase 3. A segmented, out-of-order-narration map is
    correctly discontinuous in `ts` at its own seams — real audio structure,
    not measurement error — and scoring that discontinuity as one undivided
    run inflates whole-map `density_spread` with nothing to do with how
    evenly-paced the narration itself is (measured on the real Four Past
    Midnight segmented map: whole-map 5.394 vs a max-of-per-segment 1.327).
    `max_gap_fraction` deliberately stays whole-map (segments tile the char
    space contiguously, so a char gap is real regardless of which segment it
    falls in) — every test below pins that it is unaffected by `segments`.
    """

    @staticmethod
    def _shifted_dense_map(char_start: int, span_chars: int, char_step: int,
                           rate: float, ts_start: float = 0.0) -> List[Dict]:
        """An evenly-paced map over `span_chars` chars, strictly within the
        half-open `[char_start, char_start + span_chars)` — half-open so two
        adjacent shifted maps never both land a point on the shared boundary
        char (which would leak one map's point into the other's segment
        slice and corrupt the per-segment computation)."""
        return [{"char": char_start + c, "ts": ts_start + c / rate}
                for c in range(0, span_chars, char_step)]

    @staticmethod
    def _shifted_grouped_map(char_start: int, chars_per_group: List[int],
                             rates: List[float], ts_start: float = 0.0) -> List[Dict]:
        """`_grouped_map`, offset onto `char_start` — see its docstring."""
        points: List[Dict] = []
        char_cursor = 0.0
        ts_cursor = ts_start
        for chars, rate in zip(chars_per_group, rates):
            char_step = chars / _ANCHORS_PER_GROUP
            ts_step = char_step / rate
            for _ in range(_ANCHORS_PER_GROUP):
                char_cursor += char_step
                ts_cursor += ts_step
                points.append({"char": char_start + round(char_cursor), "ts": ts_cursor})
        return points

    def test_segmented_scoring_beats_whole_map_on_a_reordered_fixture(self):
        """Four chapter-sized segments, each perfectly evenly paced on its
        own but independently placed in `ts` (unrelated offsets — the real
        shape of segments RANSAC-fit to wherever their own audio actually
        landed, not a tidy back-to-back concatenation) and deliberately
        *not* aligned to the whole-map's own 20-slice partition (187/211/
        197/203 points per segment, none a clean fraction of the 798-point
        total). Scored as one undivided run, the seam that lands inside a
        single slice with a huge, unrelated `ts` jump blows whole-map
        `density_spread` up past 50; scored per-segment, every segment is
        perfectly paced (spread ~1.0) and the aggregate follows exactly.
        """
        segment_defs = [
            (0, 50000, 12.0, 187, 44415.0),
            (50000, 50000, 14.0, 211, 500.0),
            (100000, 50000, 16.0, 197, 90000.0),
            (150000, 50000, 13.0, 203, 9000.0),
        ]
        placements: List[List[Dict]] = []
        segments: List[Dict] = []
        combined: List[Dict] = []
        for char_start, span, rate, n, ts0 in segment_defs:
            points = [{"char": char_start + int(span * i / n),
                       "ts": ts0 + int(span * i / n) / rate}
                      for i in range(n)]
            placements.append(points)
            segments.append({"char_start": char_start, "char_end": char_start + span,
                             "ts_start": points[0]["ts"], "ts_end": points[-1]["ts"]})
            combined.extend(points)
        combined.sort(key=lambda p: p["char"])

        whole = score_map(combined)
        segmented = score_map(combined, segments=segments)

        # Requirement: max_gap_fraction is untouched by `segments`.
        self.assertEqual(whole.max_gap_fraction, segmented.max_gap_fraction)

        self.assertGreater(whole.density_spread, 50.0)
        self.assertLess(segmented.density_spread, 1.01)
        self.assertGreater(segmented.score, whole.score + 0.3)

        # The per-segment aggregate is exactly the max of each segment's own
        # standalone spread — not an average, not any other combination.
        own_spreads = [score_map(points).density_spread for points in placements]
        self.assertAlmostEqual(segmented.density_spread, max(own_spreads), places=6)

    def test_one_bad_segment_among_good_ones_still_lowers_the_score(self):
        """Three perfectly-paced segments plus one carrying a single
        anomalous slow decile (the same construction
        `TestDensitySpreadDiscriminates` uses). `max` aggregation must
        report the bad segment's own spread, not the four segments'
        average — and, as a side effect, this is exactly the case whole-map
        scoring is blind to: diluted across the other 3,000 good points, the
        same anomaly that dominates its own 200-point segment vanishes into
        one of 20 GLOBAL slices and never moves the whole-map score at all.
        """
        good = [self._shifted_dense_map(offset, 100000, 100, 15.0)
                for offset in (0, 100000, 200000)]
        bad_rates = [3.0] + [15.0] * 19
        bad = self._shifted_grouped_map(300000, [100] * 20, bad_rates)

        segments = [{"char_start": s, "char_end": s + 100000} for s in (0, 100000, 200000)]
        segments.append({"char_start": 300000, "char_end": 300000 + sum([100] * 20)})

        combined = [point for group in good for point in group] + bad
        combined.sort(key=lambda p: p["char"])

        whole = score_map(combined)
        segmented = score_map(combined, segments=segments)

        good_spreads = [score_map(points).density_spread for points in good]
        bad_spread = score_map(bad).density_spread
        average_spread = (sum(good_spreads) + bad_spread) / 4

        self.assertEqual(whole.max_gap_fraction, segmented.max_gap_fraction)
        self.assertAlmostEqual(segmented.density_spread, bad_spread, places=4)
        # Proves max, not average: the average of the four per-segment
        # spreads is ~2.0, well below the bad segment's own ~5.0.
        self.assertGreater(segmented.density_spread, average_spread + 1.0)

        # Whole-map scoring dilutes the same anomaly across ~3,000 good
        # points and misses it entirely — exactly the blind spot
        # max-aggregated per-segment scoring exists to close.
        self.assertLess(whole.density_spread, 1.1)
        self.assertLess(segmented.score, whole.score - 0.1)

    def test_segments_none_reproduces_todays_score_exactly(self):
        """Compatibility guarantee: omitting `segments` (or passing an empty
        list) must be byte-identical to today's whole-map scoring, on a
        fixture already exercised elsewhere in this file."""
        amap = _dense_map(100000, 100, 100.0)
        baseline = score_map(amap)
        self.assertEqual(baseline, score_map(amap, segments=None))
        self.assertEqual(baseline, score_map(amap, segments=[]))


class TestSegmentDensityFloorAndFallback(unittest.TestCase):
    """A live production remap ("Dearest", 419,333 chars, 60 segments)
    dragged an objectively-improved map's score from 0.9758 down to 0.9099:
    the max-aggregation in `_segment_aware_density_spread` had no floor on
    how few points a segment could contribute with, so a 165-char/16-point
    segment (and other small-but-not-quite-that-tiny ones) polluted the max
    with unmeasurable or meaningless per-segment spreads. `_density_spread`
    slices into `_DENSITY_SLICE_COUNT` (20) equal-anchor-count slices --
    below `_SEGMENT_MIN_POINTS_FOR_DENSITY` (3 points/slice = 60) that
    slicing cannot mean anything.
    """

    @staticmethod
    def _shifted_dense_map(char_start: int, span_chars: int, char_step: int,
                           rate: float, ts_start: float = 0.0) -> List[Dict]:
        """Same fixture helper as `TestSegmentAwareDensitySpread` (duplicated
        rather than shared across classes, matching this file's existing
        per-class-scoped `@staticmethod` helper style)."""
        return [{"char": char_start + c, "ts": ts_start + c / rate}
                for c in range(0, span_chars, char_step)]

    def test_tiny_segment_below_floor_does_not_affect_aggregate(self):
        """A segment with fewer than `_SEGMENT_MIN_POINTS_FOR_DENSITY` points
        is excluded from the max-aggregate outright -- even though its own
        few points, taken at face value, describe a wildly erratic pace that
        would (if trusted) dominate the max. A large, evenly-paced segment
        alongside it must still score well, unaffected by the tiny one's
        presence."""
        good = self._shifted_dense_map(0, 100000, 100, 15.0)
        tiny_n = _SEGMENT_MIN_POINTS_FOR_DENSITY - 1
        # Quadratic char->ts spacing: a genuinely erratic pace, not just a
        # small evenly-paced sample -- if this were trusted it would report
        # a huge spread, not merely a noisy-but-similar one.
        tiny = [{"char": 100000 + i, "ts": 100000.0 + (i ** 2) / 10.0} for i in range(tiny_n)]

        segments = [{"char_start": 0, "char_end": 100000},
                    {"char_start": 100000, "char_end": 100000 + tiny_n}]
        combined = sorted(good + tiny, key=lambda p: p["char"])

        segmented = score_map(combined, segments=segments)
        good_alone = score_map(good)

        self.assertAlmostEqual(segmented.density_spread, good_alone.density_spread, places=6)
        self.assertGreater(segmented.score, 0.9)

    def test_non_finite_segment_spread_is_discarded_not_propagated(self):
        """A segment that clears the point-count floor but still comes back
        non-finite from `_density_spread` (here: enough points, but `ts`
        never advances, so no slice yields a usable rate) must not
        contribute `inf` to the aggregate -- the resulting score must stay
        finite and reflect only the genuinely measurable segment."""
        good = self._shifted_dense_map(0, 100000, 100, 15.0)
        flat_n = _SEGMENT_MIN_POINTS_FOR_DENSITY + 10
        flat_ts = [{"char": 100000 + i, "ts": 500000.0} for i in range(flat_n)]

        segments = [{"char_start": 0, "char_end": 100000},
                    {"char_start": 100000, "char_end": 100000 + flat_n}]
        combined = sorted(good + flat_ts, key=lambda p: p["char"])

        segmented = score_map(combined, segments=segments)
        good_alone = score_map(good)

        self.assertTrue(math.isfinite(segmented.density_spread))
        self.assertTrue(math.isfinite(segmented.score))
        self.assertAlmostEqual(segmented.density_spread, good_alone.density_spread, places=6)
        self.assertGreater(segmented.score, 0.9)

    def test_one_badly_paced_large_segment_still_drags_score_down(self):
        """The floor must not neuter `max` aggregation: a LARGE segment
        (comfortably above `_SEGMENT_MIN_POINTS_FOR_DENSITY`) that is
        genuinely badly paced still has to drag the score down, proving the
        floor excludes only unmeasurable segments, not real bad ones. Same
        seam-reset construction as
        `TestSegmentAwareDensitySpread.test_one_bad_segment_among_good_ones_still_lowers_the_score`
        (each segment's own `ts` independently starts near 0 at its own
        `char_start`, the real RANSAC-fit shape): whole-map scoring dilutes
        the bad segment's anomaly into one of only 20 global slices and
        misses it; segment-aware scoring isolates it."""
        good = [TestSegmentAwareDensitySpread._shifted_dense_map(offset, 100000, 100, 15.0)
                for offset in (0, 100000, 200000)]
        bad_rates = [3.0] + [15.0] * 19
        bad = TestSegmentAwareDensitySpread._shifted_grouped_map(300000, [100] * 20, bad_rates)
        self.assertGreaterEqual(len(bad), _SEGMENT_MIN_POINTS_FOR_DENSITY)

        segments = [{"char_start": s, "char_end": s + 100000} for s in (0, 100000, 200000)]
        segments.append({"char_start": 300000, "char_end": 300000 + sum([100] * 20)})

        combined = sorted([point for group in good for point in group] + bad,
                          key=lambda p: p["char"])

        whole = score_map(combined)
        segmented = score_map(combined, segments=segments)

        self.assertLess(segmented.score, whole.score - 0.1)
        bad_alone = score_map(bad).density_spread
        self.assertAlmostEqual(segmented.density_spread, bad_alone, places=4)

    def test_all_tiny_segments_fall_back_to_whole_map_scoring(self):
        """A book made entirely of tiny (sub-floor) segments must still get
        a real score: falling back to whole-map `_density_spread` rather
        than returning `inf` when no segment qualifies for the aggregate."""
        tiny_n = _SEGMENT_MIN_POINTS_FOR_DENSITY - 1
        segments = []
        combined = []
        cursor = 0
        for i in range(5):
            points = [{"char": cursor + c, "ts": i * 10000.0 + c / 15.0} for c in range(tiny_n)]
            combined.extend(points)
            segments.append({"char_start": cursor, "char_end": cursor + tiny_n})
            cursor += tiny_n + 1000

        combined.sort(key=lambda p: p["char"])
        whole = score_map(combined)
        segmented = score_map(combined, segments=segments)

        self.assertTrue(math.isfinite(segmented.density_spread))
        self.assertEqual(whole.density_spread, segmented.density_spread)
        self.assertEqual(whole.score, segmented.score)


class TestBackwardsFraction(unittest.TestCase):

    def test_nonzero_when_timestamps_regress_partway_through(self):
        amap = [{"char": 0, "ts": 0.0}, {"char": 100, "ts": 1.0}, {"char": 200, "ts": 2.0},
                {"char": 300, "ts": 1.5}, {"char": 400, "ts": 3.0}]
        quality = score_map(amap)
        self.assertAlmostEqual(quality.backwards_fraction, 0.25)

    def test_zero_when_timestamps_are_monotonic(self):
        amap = [{"char": 0, "ts": 0.0}, {"char": 100, "ts": 1.0}, {"char": 200, "ts": 2.0},
                {"char": 300, "ts": 2.5}, {"char": 400, "ts": 3.0}]
        quality = score_map(amap)
        self.assertEqual(quality.backwards_fraction, 0.0)


class TestExcludeSpans(unittest.TestCase):
    """Mirrors tests/test_ctc_unnarrated_spans.py's
    test_gap_fraction_subtracts_clipped_union_from_gap_and_extent."""

    def test_exclude_spans_removes_excluded_chars_from_gap_computation(self):
        points = [{"char": c, "ts": c / 10} for c in [0, 100, 200, 800, 900, 1000]]
        quality = score_map(points, exclude_spans=[(200, 800)])
        self.assertAlmostEqual(quality.max_gap_fraction, 0.25)

    def test_overlapping_exclude_spans_collapse_to_their_union(self):
        points = [{"char": c, "ts": c / 10} for c in [0, 100, 200, 800, 900, 1000]]
        quality = score_map(points, exclude_spans=[(500, 800), (200, 600), (300, 500)])
        self.assertAlmostEqual(quality.max_gap_fraction, 0.25)


class TestIsRegression(unittest.TestCase):

    def _quality(self, score: float) -> MapQuality:
        return MapQuality(anchors=10, span_chars=1000, max_gap_fraction=0.1,
                           anchor_density=5.0, density_spread=2.0,
                           backwards_fraction=0.0, score=score)

    def test_equal_scores_are_not_a_regression(self):
        # This is the case that protects the CTC upgrade path: a healthy CTC
        # challenger and a healthy lexical incumbent routinely tie on this
        # score (both look structurally perfect), and that tie must NOT be
        # treated as a regression or CTC could never replace lexical again.
        incumbent, challenger = self._quality(0.996), self._quality(0.996)
        self.assertFalse(is_regression(incumbent, challenger))

    def test_challenger_below_margin_is_a_regression(self):
        incumbent, challenger = self._quality(0.90), self._quality(0.87)
        self.assertTrue(is_regression(incumbent, challenger, margin=0.02))

    def test_challenger_within_margin_is_not_a_regression(self):
        incumbent, challenger = self._quality(0.90), self._quality(0.89)
        self.assertFalse(is_regression(incumbent, challenger, margin=0.02))

    def test_challenger_better_than_incumbent_is_never_a_regression(self):
        incumbent, challenger = self._quality(0.80), self._quality(0.95)
        self.assertFalse(is_regression(incumbent, challenger))

    def test_default_margin_is_used_when_omitted(self):
        incumbent, challenger = self._quality(0.90), self._quality(0.87)
        self.assertTrue(is_regression(incumbent, challenger))


if __name__ == "__main__":
    unittest.main()
