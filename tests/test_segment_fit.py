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

import unittest
from typing import Dict, List, Tuple

from src.services import segment_fit
from src.services.segment_fit import Segment, fit_segments

# Real measured narration speed for Tress, used to build realistic fixtures.
CHARS_PER_SEC = 13.8


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


if __name__ == "__main__":
    unittest.main()
