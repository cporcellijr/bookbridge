"""Tests for `src/services/segment_fit.py` (issue #426, Phase 0).

The two headline cases are real books, with numbers taken from
`docs/PLAN_OUT_OF_ORDER_NARRATION.md`:

* Four Past Midnight — four novellas spined 2-4-3-1, narrated 1-2-3-4. A global
  longest-increasing-subsequence filter can keep anchors from at most two of the
  four blocks (production dropped 51%); segment fitting must keep all four.
* Tress of the Emerald Sea — the acknowledgements are spined at the front
  (chars 1,598-6,173) but narrated at 44,415-44,694s, second-to-last. Production
  mapped char 3,000 to 10.7s: 12.3 hours wrong.
"""

import itertools
import json
import os
import unittest
from typing import Dict, List, Tuple

from src.services import segment_fit
from src.services.segment_fit import Segment, fit_segments, select_anchors

# Real measured narration speed for Tress, used to build realistic fixtures.
CHARS_PER_SEC = 13.8

# Real candidate anchors captured from AlignmentService._filter_monotonic_lis
# for Tress of the Emerald Sea, plus the four boundaries where seam bleed
# dropped a correctly-fit segment (issue #426). See the fixture's own `note`.
_TRESS_SEAM_FIXTURE = os.path.join(
    os.path.dirname(__file__), "fixtures", "tress_seam_anchors.json")


def make_anchors(char_start: int, char_end: int, ts_start: float, ts_end: float,
                 count: int) -> List[Dict]:
    """Evenly spaced anchors mapping a char range linearly onto a ts range."""
    anchors = []
    for i in range(count):
        f = i / max(count - 1, 1)
        anchors.append({
            "char": int(char_start + f * (char_end - char_start)),
            "ts": ts_start + f * (ts_end - ts_start),
            "t_idx": i,
            "b_idx": i,
        })
    return anchors


def longest_increasing_subsequence(anchors: List[Dict]) -> int:
    """Length of the LIS by ts over anchors sorted by char — the filter this
    module exists to replace (`AlignmentService._filter_monotonic_lis`)."""
    import bisect
    tails: List[float] = []
    for anchor in sorted(anchors, key=lambda a: a["char"]):
        ts = anchor["ts"]
        pos = bisect.bisect_left(tails, ts)
        if pos == len(tails):
            tails.append(ts)
        else:
            tails[pos] = ts
    return len(tails)


class TestDegenerateSingleBoundary(unittest.TestCase):
    def test_clean_linear_book_places_one_segment(self):
        total = 400000
        anchors = make_anchors(0, total, 0.0, total / CHARS_PER_SEC, 500)
        result = fit_segments(anchors, [(0, total)], total)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].char_start, 0)
        self.assertEqual(result[0].char_end, total)
        self.assertAlmostEqual(result[0].ts_start, 0.0, delta=5.0)
        self.assertAlmostEqual(result[0].ts_end, total / CHARS_PER_SEC, delta=5.0)


class TestInOrderBookManyBoundaries(unittest.TestCase):
    """An in-order book yields N segments (not one), and their ts order matches
    their char order — i.e. the segmentation is order-preserving, which is what
    makes it equivalent to today's single monotonic map."""

    def test_order_is_preserved(self):
        chapters = 12
        width = 30000
        total = chapters * width
        anchors: List[Dict] = []
        boundaries: List[Tuple[int, int]] = []
        for i in range(chapters):
            lo, hi = i * width, (i + 1) * width
            boundaries.append((lo, hi))
            anchors += make_anchors(lo, hi, lo / CHARS_PER_SEC, hi / CHARS_PER_SEC, 60)

        result = fit_segments(anchors, boundaries, total)
        self.assertEqual(len(result), chapters)
        by_ts = [s.char_start for s in sorted(result, key=lambda s: s.ts_start)]
        by_char = [s.char_start for s in sorted(result, key=lambda s: s.char_start)]
        self.assertEqual(by_ts, by_char)


class TestFourPastMidnightPermutation(unittest.TestCase):
    """The case the module exists for: EPUB order 2-4-3-1, narration 1-2-3-4."""

    def _build(self):
        width = 100000
        per_block_seconds = width / CHARS_PER_SEC
        # spine slot -> which novella (0-based) sits there
        spine_to_novella = {0: 1, 1: 3, 2: 2, 3: 0}
        anchors: List[Dict] = []
        boundaries: List[Tuple[int, int]] = []
        for slot, novella in spine_to_novella.items():
            lo, hi = slot * width, (slot + 1) * width
            boundaries.append((lo, hi))
            anchors += make_anchors(lo, hi,
                                    novella * per_block_seconds,
                                    (novella + 1) * per_block_seconds, 500)
        return anchors, boundaries, 4 * width, spine_to_novella, per_block_seconds

    def test_all_four_blocks_are_placed_in_narration_order(self):
        anchors, boundaries, total, spine_to_novella, per_block = self._build()
        result = fit_segments(anchors, boundaries, total)

        self.assertEqual(len(result), 4, "all four novellas must place")
        # returned sorted by ts_start => narration order => spine slots 3,0,2,1
        self.assertEqual([s.char_start // 100000 for s in result], [3, 0, 2, 1])
        for seg in result:
            novella = spine_to_novella[seg.char_start // 100000]
            self.assertAlmostEqual(seg.ts_start, novella * per_block, delta=30.0)

    def test_beats_the_global_lis_it_replaces(self):
        anchors, boundaries, total, _, _ = self._build()
        result = fit_segments(anchors, boundaries, total)

        kept_by_segments = sum(s.inliers for s in result)
        kept_by_lis = longest_increasing_subsequence(anchors)
        self.assertGreater(kept_by_segments, 1.8 * kept_by_lis,
                           f"segments kept {kept_by_segments}, LIS kept {kept_by_lis}")
        self.assertGreater(kept_by_segments, 0.95 * len(anchors))


class TestTressAcknowledgements(unittest.TestCase):
    """A small block at the front of the char space, narrated second-to-last."""

    def test_small_front_block_places_at_its_true_late_timestamp(self):
        total = 618629
        ack_lo, ack_hi = 1598, 6173
        body_lo, body_hi = 6173, 616016
        anchors = (make_anchors(ack_lo, ack_hi, 44415.0, 44694.1, 120)
                   + make_anchors(body_lo, body_hi, 32.5, 44146.5, 3000))
        boundaries = [(0, ack_lo), (ack_lo, ack_hi), (body_lo, body_hi)]

        result = fit_segments(anchors, boundaries, total)
        acks = [s for s in result if s.char_start == ack_lo]
        self.assertEqual(len(acks), 1, "the acknowledgements must be placed")
        self.assertGreater(acks[0].ts_start, 44000.0,
                           "acknowledgements must land late, not at the front")
        self.assertLess(abs(acks[0].ts_start - 44415.0), 30.0)

    def test_front_matter_without_anchors_is_left_unplaced(self):
        total = 618629
        anchors = make_anchors(6173, 616016, 32.5, 44146.5, 3000)
        boundaries = [(0, 1598), (1598, 6173), (6173, 616016)]
        result = fit_segments(anchors, boundaries, total)
        self.assertEqual([s.char_start for s in result], [6173])


class TestUnplacedAndNoise(unittest.TestCase):
    def test_noise_boundary_is_rejected_and_others_still_place(self):
        import random
        rng = random.Random(7)
        total = 200000
        good = make_anchors(100000, 200000, 7246.0, 14493.0, 400)
        noise = [{"char": rng.randrange(0, 100000), "ts": rng.uniform(0.0, 14493.0),
                  "t_idx": i, "b_idx": i} for i in range(400)]
        result = fit_segments(good + noise, [(0, 100000), (100000, 200000)], total)
        self.assertEqual([s.char_start for s in result], [100000],
                         "the noise boundary must not place")

    def test_contaminated_boundary_is_rejected_on_inlier_fraction(self):
        """A boundary where a clean line explains only a minority of its own
        candidates must not place: the consensus is real but the evidence is
        mostly something else. This is what `_MIN_INLIER_FRACTION` is for -
        without it this boundary places on 20% of its anchors."""
        import random
        rng = random.Random(11)
        collinear = make_anchors(0, 100000, 0.0, 7246.0, 30)
        scattered = [{"char": rng.randrange(0, 100000), "ts": rng.uniform(0.0, 40000.0)}
                     for _ in range(120)]
        self.assertEqual(fit_segments(collinear + scattered, [(0, 100000)], 100000), [])

    def test_physically_implausible_narration_speed_is_rejected(self):
        """Perfectly collinear anchors at an impossible chars/sec are anchors
        that leaked in from elsewhere, not narration. This is what the
        `_MIN_CHARS_PER_SEC`/`_MAX_CHARS_PER_SEC` band is for."""
        too_slow = make_anchors(0, 100000, 0.0, 2000000.0, 60)   # 0.05 chars/sec
        too_fast = make_anchors(0, 100000, 0.0, 500.0, 60)       # 200 chars/sec
        self.assertEqual(fit_segments(too_slow, [(0, 100000)], 100000), [])
        self.assertEqual(fit_segments(too_fast, [(0, 100000)], 100000), [])

    def test_too_few_anchors_is_unplaced(self):
        """Five clean collinear anchors are still too little evidence to place a
        segment. The count is hardcoded on purpose: deriving it from
        `_MIN_SEGMENT_INLIERS` would make the test track the constant and pass
        no matter how low the constant was set."""
        total = 100000
        self.assertGreater(segment_fit._MIN_SEGMENT_INLIERS, 5)
        anchors = make_anchors(0, total, 0.0, 7246.0, 5)
        self.assertEqual(fit_segments(anchors, [(0, total)], total), [])


class TestConflictResolution(unittest.TestCase):
    def test_stronger_fit_survives_and_invariant_holds(self):
        total = 200000
        # Both claim the same audio window, at a realistic narration speed
        # (100,000 chars over ~7,246s); a 1,000s window would be 100 chars/s
        # and correctly rejected by the sane-speed band before any conflict.
        strong = make_anchors(0, 100000, 1000.0, 8246.0, 400)
        weak = make_anchors(100000, 200000, 1000.0, 8246.0, 20)
        result = fit_segments(strong + weak, [(0, 100000), (100000, 200000)], total)
        self.assertEqual(len(result), 1, "overlapping audio must resolve to one")
        self.assertEqual(result[0].char_start, 0)
        # 399, not 400: the last generated anchor sits at char 100000 exactly,
        # which the half-open boundary [0, 100000) excludes.
        self.assertEqual(result[0].inliers, 399)

    def test_returned_segments_are_disjoint_in_both_axes(self):
        total = 400000
        anchors: List[Dict] = []
        boundaries: List[Tuple[int, int]] = []
        for slot, novella in {0: 1, 1: 3, 2: 2, 3: 0}.items():
            lo, hi = slot * 100000, (slot + 1) * 100000
            boundaries.append((lo, hi))
            anchors += make_anchors(lo, hi, novella * 7246.0, (novella + 1) * 7246.0, 300)
        result = fit_segments(anchors, boundaries, total)
        for i, first in enumerate(result):
            for second in result[i + 1:]:
                self.assertTrue(first.char_end <= second.char_start
                                or second.char_end <= first.char_start)
                self.assertTrue(first.ts_end <= second.ts_start
                                or second.ts_end <= first.ts_start)


class TestSelectAnchors(unittest.TestCase):
    """`select_anchors` re-derives a placement's inliers, since `Segment` keeps
    only their count. The tolerance filter is what makes it a re-derivation
    rather than "everything in the char range"."""

    def test_outliers_inside_the_char_range_are_not_retained(self):
        good = make_anchors(0, 100000, 0.0, 7246.0, 50)
        # Same char range, but pointing at audio 5+ hours away: duplicate n-gram
        # matches from elsewhere in the book. Dropping the tolerance check would
        # sweep every one of these into the map.
        outliers = [{"char": c, "ts": 40000.0} for c in range(500, 100000, 2000)]
        placement = Segment(char_start=0, char_end=100000, ts_start=0.0,
                            ts_end=7246.0, inliers=50, residual=0.1)

        kept = select_anchors(good + outliers, [placement])

        self.assertLess(len(kept), len(good) + len(outliers),
                        "outliers must not be retained")
        self.assertTrue(all(abs(a["ts"] - 40000.0) > 1.0 for a in kept),
                        "no far-off-line anchor may survive")
        self.assertEqual(kept, sorted(kept, key=lambda a: a["char"]))

    def test_retained_anchors_come_only_from_placed_ranges(self):
        inside = make_anchors(0, 50000, 0.0, 3623.0, 40)
        outside = make_anchors(60000, 100000, 4300.0, 7246.0, 40)
        placement = Segment(char_start=0, char_end=50000, ts_start=0.0,
                            ts_end=3623.0, inliers=40, residual=0.1)
        kept = select_anchors(inside + outside, [placement])
        self.assertTrue(all(a["char"] < 50000 for a in kept))


class TestDeterminism(unittest.TestCase):
    def test_identical_input_gives_identical_output_across_runs(self):
        """Two equal-sized collinear populations in one boundary is a genuine
        tie, and `_ransac_fit_boundary` keeps the first line that reaches the
        winning inlier count — so the winner depends on sample order and only
        the fixed seed makes it reproducible. Clean unambiguous data converges
        to the same answer whatever the seed, and would not test this at all.
        """
        total = 100000
        population_a = make_anchors(0, total, 0.0, 7246.0, 100)
        population_b = [{"char": a["char"], "ts": a["ts"] + 3000.0}
                        for a in make_anchors(0, total, 0.0, 7246.0, 100)]
        anchors = population_a + population_b

        first = fit_segments(anchors, [(0, total)], total)
        self.assertEqual(len(first), 1)
        for _ in range(99):
            self.assertEqual(fit_segments(anchors, [(0, total)], total), first)


class TestRobustness(unittest.TestCase):
    def test_degenerate_inputs_return_empty_and_never_raise(self):
        good = make_anchors(0, 1000, 0.0, 72.0, 50)
        for anchors, boundaries, total in (
            ([], [(0, 1000)], 1000),
            (good, [], 1000),
            (good, [(0, 1000)], 0),
            ([{"char": 5, "ts": 1.0}], [(0, 1000)], 1000),
            ([{"char": 5, "ts": float(i)} for i in range(50)], [(0, 1000)], 1000),
            (good, [(1000, 0)], 1000),
            (good, [(0, 999999)], 1000),
        ):
            self.assertEqual(fit_segments(anchors, boundaries, total), [])

    def test_global_char_key_is_preferred_over_char(self):
        anchors = [{"char": 0, "global_char": c, "ts": c / CHARS_PER_SEC}
                   for c in range(0, 100000, 200)]
        result = fit_segments(anchors, [(0, 100000)], 100000)
        self.assertEqual(len(result), 1)
        self.assertAlmostEqual(result[0].ts_end, 100000 / CHARS_PER_SEC, delta=30.0)


class TestTressRealFixtureRegression(unittest.TestCase):
    """The regression test that matters: real candidate anchors captured from
    the live pipeline for Tress of the Emerald Sea, including the seam that
    dropped the acknowledgements entirely (issue #426). This is the point of
    the whole feature — assert the segment is present, not merely that a
    count is high.
    """

    def test_acknowledgements_boundary_is_placed_near_ground_truth(self):
        with open(_TRESS_SEAM_FIXTURE, encoding="utf-8") as f:
            fixture = json.load(f)

        anchors = fixture["anchors"]
        boundaries = [tuple(b) for b in fixture["boundaries"]]
        total_chars = fixture["total_chars"]

        result = fit_segments(anchors, boundaries, total_chars)

        ack_boundary = (1604, 6179)
        placed = [s for s in result if (s.char_start, s.char_end) == ack_boundary]
        self.assertEqual(len(placed), 1,
                          "the acknowledgements boundary [1604, 6179] must be placed, "
                          f"got segments: {result}")
        # Ground truth from the M4B cue sheet: 44415.0-44694.1s.
        self.assertLess(abs(placed[0].ts_start - 44415.0), 30.0,
                        f"acknowledgements ts_start {placed[0].ts_start} not near 44415.0s")

    def test_all_four_real_boundaries_place_and_stay_disjoint(self):
        """Every boundary in the fixture is a real, correctly-fit segment
        (verified in isolation against the real anchors); none should be
        dropped as a seam-bleed false positive."""
        with open(_TRESS_SEAM_FIXTURE, encoding="utf-8") as f:
            fixture = json.load(f)
        anchors = fixture["anchors"]
        boundaries = [tuple(b) for b in fixture["boundaries"]]
        total_chars = fixture["total_chars"]

        result = fit_segments(anchors, boundaries, total_chars)

        self.assertEqual(len(result), len(boundaries),
                         f"expected all {len(boundaries)} real boundaries placed, got {result}")
        segment_fit._assert_disjoint(result)

    def test_no_returned_pair_overlaps_in_ts_by_even_a_fraction_of_a_second(self):
        """The invariant `_assert_disjoint` already enforces, re-checked
        explicitly and exhaustively over every pair on real data, with a
        failure message that names the offending pair instead of just
        tripping an assert buried inside `fit_segments`. This is the
        regression test for the 12-pair-overlap bug: `_ts_conflicts` used to
        let an overlap through whenever it failed the 2%-of-shorter-segment
        fractional gate, even though the absolute-seconds floor was cleared
        (measured on the full 81-boundary book: up to 9.64s over a ~583s
        segment, comfortably real but only 1.65% of the shorter segment)."""
        with open(_TRESS_SEAM_FIXTURE, encoding="utf-8") as f:
            fixture = json.load(f)
        anchors = fixture["anchors"]
        boundaries = [tuple(b) for b in fixture["boundaries"]]
        total_chars = fixture["total_chars"]

        result = fit_segments(anchors, boundaries, total_chars)
        self.assertGreater(len(result), 1, "need at least two placed segments to test overlap")

        for first, second in itertools.combinations(result, 2):
            overlap = segment_fit._ts_overlap_seconds(first, second)
            self.assertLessEqual(overlap, 0.0,
                                 f"segments overlap by {overlap:.3f}s: {first} vs {second}")


class TestSeamBleedTrimming(unittest.TestCase):
    """Overlap small relative to the shorter segment is ordinary least-squares
    seam bleed (measured on Tress: 2.6%-6.3% of the shorter segment's own
    duration) — both segments must survive, trimmed to the midpoint of the
    disputed span."""

    def test_seam_bleed_trims_both_segments_and_keeps_both(self):
        # 10s overlap over a 300s shorter segment = 3.33%, comparable to the
        # largest real Tress seam (6.3%) and well under
        # `_SEAM_BLEED_MAX_OVERLAP_FRACTION` (15%).
        earlier = Segment(char_start=0, char_end=100, ts_start=0.0, ts_end=300.0,
                          inliers=50, residual=1.0)
        later = Segment(char_start=100, char_end=200, ts_start=290.0, ts_end=600.0,
                        inliers=40, residual=1.0)

        result = segment_fit._resolve_conflicts([earlier, later])

        self.assertEqual(len(result), 2, "both segments must survive a seam-bleed conflict")
        by_char = sorted(result, key=lambda s: s.char_start)
        midpoint = (earlier.ts_end + later.ts_start) / 2.0
        self.assertEqual(by_char[0].ts_end, midpoint)
        self.assertEqual(by_char[1].ts_start, midpoint)
        # Char ranges are untouched by trimming — only ts moves.
        self.assertEqual(by_char[0].char_start, 0)
        self.assertEqual(by_char[0].char_end, 100)
        self.assertEqual(by_char[1].char_start, 100)
        self.assertEqual(by_char[1].char_end, 200)
        segment_fit._assert_disjoint(result)

    def test_trim_to_seam_midpoint_shared_edge_is_midpoint_of_overlap(self):
        """Direct unit test of the trim helper against the real Tress
        acknowledgements/postscript seam numbers."""
        winner = Segment(char_start=610678, char_end=615320, ts_start=44153.9,
                         ts_end=44413.1, inliers=495, residual=0.71)
        ack = Segment(char_start=1604, char_end=6179, ts_start=44404.5,
                     ts_end=44683.2, inliers=275, residual=4.56)

        trimmed = segment_fit._trim_to_seam_midpoint(winner, ack)
        self.assertIsNotNone(trimmed)
        trimmed_winner, trimmed_ack = trimmed
        expected_midpoint = (44413.1 + 44404.5) / 2.0
        self.assertAlmostEqual(trimmed_winner.ts_end, expected_midpoint)
        self.assertAlmostEqual(trimmed_ack.ts_start, expected_midpoint)
        self.assertEqual(trimmed_winner.ts_start, winner.ts_start)
        self.assertEqual(trimmed_ack.ts_end, ack.ts_end)

    def test_trim_to_seam_midpoint_accepts_either_argument_order(self):
        earlier = Segment(char_start=0, char_end=100, ts_start=0.0, ts_end=300.0,
                          inliers=50, residual=1.0)
        later = Segment(char_start=100, char_end=200, ts_start=290.0, ts_end=600.0,
                        inliers=40, residual=1.0)
        forward = segment_fit._trim_to_seam_midpoint(earlier, later)
        backward = segment_fit._trim_to_seam_midpoint(later, earlier)
        self.assertEqual(forward, backward)


class TestGenuineCollisionStillDropsTheWeaker(unittest.TestCase):
    """Overlap large relative to the shorter segment is a genuine collision —
    one segment substantially claiming another's audio — and today's
    drop-the-weaker behaviour must still apply. `_is_stronger` itself is
    intentionally untouched by this fix."""

    def test_large_partial_overlap_drops_the_weaker_segment(self):
        # 50s overlap over a 300s shorter segment = 16.7%, just above
        # `_SEAM_BLEED_MAX_OVERLAP_FRACTION` (15%): a genuine collision.
        weak = Segment(char_start=0, char_end=100, ts_start=0.0, ts_end=300.0,
                      inliers=50, residual=1.0)
        strong = Segment(char_start=100, char_end=200, ts_start=250.0, ts_end=550.0,
                         inliers=80, residual=0.5)

        result = segment_fit._resolve_conflicts([weak, strong])

        self.assertEqual(len(result), 1, "a genuine collision must still drop the weaker segment")
        self.assertIs(result[0], strong)

    def test_full_overlap_between_two_full_fit_segments_still_resolves_to_one(self):
        """Unchanged coverage of the pre-existing behaviour this fix must not
        disturb: two segments claiming the exact same audio window."""
        total = 200000
        strong = make_anchors(0, 100000, 1000.0, 8246.0, 400)
        weak = make_anchors(100000, 200000, 1000.0, 8246.0, 20)
        result = fit_segments(strong + weak, [(0, 100000), (100000, 200000)], total)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].char_start, 0)


class TestTrimNeverInverts(unittest.TestCase):
    """Trimming must never produce an inverted or zero-length segment; a
    disputed span that would require that is a genuine collision, not
    bleed."""

    def test_full_containment_refuses_to_trim(self):
        earlier = Segment(char_start=0, char_end=100, ts_start=0.0, ts_end=1000.0,
                          inliers=50, residual=1.0)
        later = Segment(char_start=100, char_end=200, ts_start=400.0, ts_end=600.0,
                        inliers=20, residual=1.0)
        self.assertIsNone(segment_fit._trim_to_seam_midpoint(earlier, later))

    def test_midpoint_exactly_on_an_edge_refuses_to_trim(self):
        """Boundary case: the midpoint lands exactly on `later.ts_end` (not
        just past it) — still must refuse rather than emit a zero-length
        `later` segment."""
        earlier = Segment(char_start=0, char_end=100, ts_start=0.0, ts_end=1000.0,
                          inliers=50, residual=1.0)
        later = Segment(char_start=100, char_end=200, ts_start=200.0, ts_end=600.0,
                        inliers=50, residual=1.0)
        # midpoint = (1000 + 200) / 2 = 600 == later.ts_end exactly.
        trimmed = segment_fit._trim_to_seam_midpoint(earlier, later)
        self.assertIsNone(trimmed)

    def test_resolve_conflicts_falls_back_to_dropping_weaker_when_trim_would_invert(self):
        """`_is_seam_bleed` currently guarantees `_trim_to_seam_midpoint` never
        returns None for anything it classifies as bleed: inverting the trim
        needs an overlap of at least 2x the shorter segment's own duration
        (a >=200% overlap fraction), far above
        `_SEAM_BLEED_MAX_OVERLAP_FRACTION` (15%) — so this fallback branch in
        `_resolve_conflicts` cannot be reached by any real fit today. Widen
        the threshold here (restored in `finally`) to exercise the branch
        directly and prove it still degrades safely to genuine-collision
        handling instead of ever emitting an inverted segment.
        """
        earlier = Segment(char_start=0, char_end=100, ts_start=0.0, ts_end=1000.0,
                          inliers=50, residual=1.0)
        later = Segment(char_start=100, char_end=200, ts_start=400.0, ts_end=600.0,
                        inliers=90, residual=0.5)
        original_fraction = segment_fit._SEAM_BLEED_MAX_OVERLAP_FRACTION
        segment_fit._SEAM_BLEED_MAX_OVERLAP_FRACTION = 3.0
        try:
            self.assertTrue(segment_fit._is_seam_bleed(earlier, later),
                            "widened threshold must classify full containment as bleed")
            self.assertIsNone(segment_fit._trim_to_seam_midpoint(earlier, later))
            result = segment_fit._resolve_conflicts([earlier, later])
        finally:
            segment_fit._SEAM_BLEED_MAX_OVERLAP_FRACTION = original_fraction

        self.assertEqual(len(result), 1)
        self.assertIs(result[0], later, "later wins on inliers (90 > 50)")
        for seg in result:
            self.assertLess(seg.ts_start, seg.ts_end, "no inverted or zero-length segment")


class TestSubFractionalOverlapNowTrimmed(unittest.TestCase):
    """The exact shape that slipped through the deleted `_ts_conflicts` gate:
    an overlap large enough in absolute seconds (> the old 5.0s floor) but
    below the old 2%-of-shorter-segment fractional floor, so the old AND-of-
    both-thresholds gate called it "not a conflict" and left both segments
    overlapping. Measured on the real 81-boundary book: up to 9.64s over a
    ~583s segment (1.65%). This must now be trimmed like any other bleed."""

    def test_overlap_below_old_fractional_gate_is_trimmed_not_ignored(self):
        earlier = Segment(char_start=0, char_end=1000, ts_start=0.0, ts_end=583.0,
                          inliers=60, residual=1.0)
        later = Segment(char_start=1000, char_end=2000, ts_start=573.36, ts_end=1273.36,
                        inliers=55, residual=1.0)

        overlap = segment_fit._ts_overlap_seconds(earlier, later)
        shortest = min(earlier.ts_end - earlier.ts_start, later.ts_end - later.ts_start)
        # Confirms this fixture actually reproduces the reported gap: clears
        # the old 5.0s absolute floor but falls short of the old 2% fractional
        # floor - the old `_ts_conflicts` would have returned False here.
        self.assertGreater(overlap, 5.0)
        self.assertLess(overlap / shortest, 0.02)

        result = segment_fit._resolve_conflicts([earlier, later])

        self.assertEqual(len(result), 2, "both segments must survive - this is bleed, not collision")
        by_char = sorted(result, key=lambda s: s.char_start)
        midpoint = (earlier.ts_end + later.ts_start) / 2.0
        self.assertEqual(by_char[0].ts_end, midpoint)
        self.assertEqual(by_char[1].ts_start, midpoint)
        segment_fit._assert_disjoint(result)


class TestChainOfBleedingSeamsAllResolve(unittest.TestCase):
    """A chain of four consecutive segments, each bleeding into the next by
    5s, all trim correctly and end up strictly disjoint. `_resolve_conflicts`
    only ever compares a candidate against `kept[-1]`; this is the shape most
    likely to expose a bug if that were ever insufficient - trimming
    `kept[-1]` against the third segment could, in principle, reopen a
    conflict with the second. It does not: see the invariant argument in
    `_resolve_conflicts`'s own docstring."""

    def test_four_consecutive_bleeding_seams_trim_to_a_fully_disjoint_chain(self):
        a = Segment(char_start=0, char_end=1000, ts_start=0.0, ts_end=300.0,
                    inliers=60, residual=1.0)
        b = Segment(char_start=1000, char_end=2000, ts_start=295.0, ts_end=620.0,
                    inliers=55, residual=1.0)
        c = Segment(char_start=2000, char_end=3000, ts_start=615.0, ts_end=940.0,
                    inliers=50, residual=1.0)
        d = Segment(char_start=3000, char_end=4000, ts_start=935.0, ts_end=1260.0,
                    inliers=45, residual=1.0)

        result = segment_fit._resolve_conflicts([a, b, c, d])

        self.assertEqual(len(result), 4, "all four segments must survive - every seam is bleed")
        by_char = sorted(result, key=lambda s: s.char_start)
        # Each seam trims to the midpoint of its own disputed span, and nothing
        # about resolving seam B-C or C-D disturbs the already-settled A-B seam.
        self.assertEqual(by_char[0].ts_end, 297.5)
        self.assertEqual(by_char[1].ts_start, 297.5)
        self.assertEqual(by_char[1].ts_end, 617.5)
        self.assertEqual(by_char[2].ts_start, 617.5)
        self.assertEqual(by_char[2].ts_end, 937.5)
        self.assertEqual(by_char[3].ts_start, 937.5)
        segment_fit._assert_disjoint(result)
        for first, second in itertools.combinations(result, 2):
            self.assertLessEqual(segment_fit._ts_overlap_seconds(first, second), 0.0)


class TestAssertDisjointHasNoTolerance(unittest.TestCase):
    """`_assert_disjoint` must reject ANY positive ts overlap, not just a
    "material" one. The deleted `_ts_conflicts` gate (and its constants,
    `_TS_CONFLICT_MIN_SECONDS`/`_TS_CONFLICT_MIN_FRACTION`) would have let an
    overlap this small through silently - this is what "true disjointness,
    no tolerance" means in practice."""

    def test_tiny_ts_overlap_below_every_old_threshold_still_raises(self):
        first = Segment(char_start=0, char_end=100, ts_start=0.0, ts_end=100.0,
                        inliers=50, residual=1.0)
        second = Segment(char_start=100, char_end=200, ts_start=99.999, ts_end=200.0,
                         inliers=50, residual=1.0)
        with self.assertRaises(AssertionError):
            segment_fit._assert_disjoint([first, second])

    def test_exact_touch_is_allowed(self):
        first = Segment(char_start=0, char_end=100, ts_start=0.0, ts_end=100.0,
                        inliers=50, residual=1.0)
        second = Segment(char_start=100, char_end=200, ts_start=100.0, ts_end=200.0,
                         inliers=50, residual=1.0)
        segment_fit._assert_disjoint([first, second])  # must not raise


if __name__ == "__main__":
    unittest.main()
