"""Tests for segment-aware alignment map lookups (issue #426, Phase 1).

Segments let a book's audiobook narration order differ from its EPUB spine
order (Four Past Midnight: spined 2-4-3-1, narrated 1-2-3-4) without
corrupting the two public lookups. `alignment_map_json` stays one flat list
sorted by `char`; `segments_json` (nullable) records which char ranges were
independently fit to which audio ranges, and is the only thing that makes the
flat list's two binary searches (`get_time_for_text` on `char`,
`get_char_for_time` on `ts`) safe once that list is no longer globally sorted
by `ts` too. See `docs/PLAN_OUT_OF_ORDER_NARRATION.md`, "Why the LIS is not
simply wrong".

Shared fixture used throughout (except the migration tests):

    Spine order (char):    segment_early [0, 100)  -- gap [100, 200) --  segment_late [200, 300)
    Narration order (ts):  segment_late is narrated FIRST  (ts [0, 100))
                           segment_early is narrated SECOND (ts [100, 200))

The [100, 200) char range has no matching audio at all (e.g. a mid-book
appendix): neither segment covers it, and no flat-map point falls in it.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import sqlalchemy as sa

from src.db.models import BookAlignment, BookAlignmentBackup
from src.services.alignment_service import AlignmentService
from src.services.segment_fit import Segment
from src.utils.polisher import Polisher

SEGMENTS = [
    {"char_start": 200, "char_end": 300, "ts_start": 0.0, "ts_end": 100.0},   # late-in-book, narrated first
    {"char_start": 0, "char_end": 100, "ts_start": 100.0, "ts_end": 200.0},   # early-in-book, narrated second
]

FLAT_MAP = [
    {"char": 0, "ts": 100.0}, {"char": 25, "ts": 125.0}, {"char": 50, "ts": 150.0},
    {"char": 75, "ts": 175.0}, {"char": 99, "ts": 199.0},
    {"char": 200, "ts": 0.0}, {"char": 225, "ts": 25.0}, {"char": 250, "ts": 50.0},
    {"char": 275, "ts": 75.0}, {"char": 299, "ts": 99.0},
]


def _stub_row(mock_db, alignment_map, segments=None):
    """Wire a MagicMock DatabaseService to return one BookAlignment-shaped
    row for every abs_id, mirroring `_stub_alignment_row` in
    tests/test_alignment_service.py, extended with `segments_json`."""
    session = mock_db.get_session()
    session.__enter__.return_value = session
    entry = MagicMock()
    entry.alignment_map_json = json.dumps(alignment_map)
    entry.segments_json = json.dumps(segments) if segments is not None else None
    session.query.return_value.filter_by.return_value.first.return_value = entry
    return session


class TestSegmentedMapRoundTrip(unittest.TestCase):
    """A hand-built 2-segment reordered map resolves correctly in both
    directions."""

    def setUp(self):
        self.mock_db = MagicMock()
        _stub_row(self.mock_db, FLAT_MAP, SEGMENTS)
        self.service = AlignmentService(self.mock_db, Polisher())

    def test_char_to_ts_interpolates_within_the_owning_segment(self):
        # char 60 is inside segment_early (ts = char + 100); its bracketing
        # points (char 50/ts150, char 75/ts175) belong to the same segment,
        # so this is a normal in-segment interpolation, not a clamp.
        ts = self.service.get_time_for_text("book", "q", char_offset_hint=60)
        self.assertEqual(ts, 160.0)

    def test_ts_to_char_interpolates_within_the_owning_segment(self):
        # ts 160 is inside segment_early's ts range; the char slice for that
        # segment alone is ts-ascending, so this must resolve to char 60 --
        # the exact inverse of the char->ts case above.
        char = self.service.get_char_for_time("book", 160.0)
        self.assertEqual(char, 60)


class TestFlatSearchIsFalsifiedBySegments(unittest.TestCase):
    """The falsification that matters: on this reordered map, the OLD flat
    ts binary search returns a wrong char, and the new segment-aware path
    returns the right one."""

    def test_old_flat_search_returns_the_specific_wrong_char(self):
        # ts=160 only appears in the second half of the flat list's ts
        # column (segment_early's, at the far char end of the map). A search
        # that assumes ts ascends across the whole list sees 160 >= the
        # LAST point's ts (99) and short-circuits to the LAST point's char --
        # 299, on the wrong side of the reordering entirely.
        old_wrong_char = AlignmentService._interpolate_within(FLAT_MAP, 160.0)
        self.assertEqual(old_wrong_char, 299)

    def test_new_segment_aware_path_returns_the_correct_char(self):
        correct_char = AlignmentService._interpolate_char_for_time(FLAT_MAP, 160.0, SEGMENTS)
        self.assertEqual(correct_char, 60)
        self.assertNotEqual(correct_char, AlignmentService._interpolate_within(FLAT_MAP, 160.0))


class TestBoundaryClamping(unittest.TestCase):
    """Never interpolate across a segment boundary; clamp to the nearest
    segment edge instead."""

    def setUp(self):
        self.mock_db = MagicMock()
        _stub_row(self.mock_db, FLAT_MAP, SEGMENTS)
        self.service = AlignmentService(self.mock_db, Polisher())

    def test_bracketing_points_straddling_a_boundary_clamp_not_interpolate(self):
        # char 99 is the LAST char of segment_early. Its ceiling bracket
        # point (char 200) belongs to segment_late -- a different segment --
        # so this must clamp to segment_early's own nearer edge (ts_end,
        # since char 99 is 1 char from char_end=100 but 99 chars from
        # char_start=0), not interpolate into segment_late's timeline.
        ts = self.service.get_time_for_text("book", "q", char_offset_hint=99)
        self.assertEqual(ts, 200.0)

    def test_char_in_the_gap_between_segments_clamps_to_nearest_edge(self):
        # char 140 falls in [100, 200) -- claimed by neither segment. Its
        # nearest segment edge is segment_early's char_end=100 (distance 40),
        # closer than segment_late's char_start=200 (distance 60).
        ts = self.service.get_time_for_text("book", "q", char_offset_hint=140)
        self.assertEqual(ts, 200.0)

    def test_removing_the_clamp_would_have_interpolated_across_the_boundary(self):
        """Falsification for the clamp itself: locks in what the naive
        (unclamped) interpolation between char 99 (segment_early) and char
        200 (segment_late) would have produced, so a regression that deletes
        the straddle check is caught by a value assertion -- not just an
        assertion that *some* clamp function got called."""
        p1, p2 = FLAT_MAP[4], FLAT_MAP[5]  # char 99/ts199, char 200/ts0
        char_span = p2['char'] - p1['char']
        time_span = p2['ts'] - p1['ts']
        naive_interpolated = p1['ts'] + time_span * ((99 - p1['char']) / char_span)
        self.assertEqual(naive_interpolated, 199.0)  # the wrong answer the clamp prevents

        clamped = self.service.get_time_for_text("book", "q", char_offset_hint=99)
        self.assertEqual(clamped, 200.0)
        self.assertNotEqual(clamped, naive_interpolated)


class TestTimestampInNoSegment(unittest.TestCase):
    """A ts inside no segment (audio with no matching text, e.g. credits)
    returns the nearest segment edge."""

    def test_ts_beyond_every_segment_resolves_to_the_nearest_edge_char(self):
        # ts=220 is past both segments' ts ranges ([0,100) and [100,200)).
        # The nearest edge is segment_early's ts_end=200.0 (distance 20),
        # closer than its ts_start=100.0 (distance 120) or either of
        # segment_late's edges (distance 120, 220).
        char = AlignmentService._interpolate_char_for_time(FLAT_MAP, 220.0, SEGMENTS)
        self.assertEqual(char, 100)


class TestProgressForTimeRespectsSegments(unittest.TestCase):
    """`get_progress_for_time` shares `_interpolate_char_for_time` with
    `get_char_for_time` and must be given segments too, or a segmented
    book's progress fraction silently uses the flat (wrong) search."""

    def test_progress_uses_the_segment_aware_char_lookup(self):
        mock_db = MagicMock()
        _stub_row(mock_db, FLAT_MAP, SEGMENTS)
        mock_db.get_alignment_total_chars.return_value = 300
        service = AlignmentService(mock_db, Polisher())

        # ts=160 -> char=60 (segment-aware) -> 60/300 = 0.20. The old flat
        # search would have produced char=299 -> ~0.997 instead.
        progress = service.get_progress_for_time("book", 160.0)
        self.assertAlmostEqual(progress, 0.20)


class TestNullSegmentsJsonUnchanged(unittest.TestCase):
    """`segments_json IS NULL` reproduces today's behaviour byte-for-byte,
    and `_save_alignment(segments=None)` never wipes a stored value."""

    def setUp(self):
        self.mock_db = MagicMock()

    def test_lookups_are_unaffected_when_segments_is_none(self):
        _stub_row(self.mock_db, FLAT_MAP, segments=None)
        service = AlignmentService(self.mock_db, Polisher())

        # Same reordered flat map as every other test in this file, but now
        # with NO segment index -- the legacy path must run, blending
        # straight across the reordering exactly as it always has. This is
        # the compatibility guarantee, not a claim the legacy answer is
        # *correct* for a reordered book.
        char = service.get_char_for_time("book", 160.0)
        self.assertEqual(char, AlignmentService._interpolate_within(FLAT_MAP, 160.0))
        self.assertEqual(char, 299)

    def test_empty_segments_list_behaves_like_none(self):
        """A stored `segments_json` of "[]" (a caller that fit zero placeable
        segments) must fall back to the legacy path exactly like NULL, not
        be treated as "has segments" with nothing in it."""
        _stub_row(self.mock_db, FLAT_MAP, segments=[])
        service = AlignmentService(self.mock_db, Polisher())

        char = service.get_char_for_time("book", 160.0)
        self.assertEqual(char, AlignmentService._interpolate_within(FLAT_MAP, 160.0))

        ts = service.get_time_for_text("book", "q", char_offset_hint=99)
        p1, p2 = FLAT_MAP[4], FLAT_MAP[5]
        naive = p1['ts'] + (p2['ts'] - p1['ts']) * ((99 - p1['char']) / (p2['char'] - p1['char']))
        self.assertEqual(ts, naive)

    def test_save_alignment_clears_segments_when_the_map_is_replaced(self):
        """Segments describe the map they were stored with, and this method
        always replaces the map, so a caller supplying none must CLEAR them.

        `total_chars` and `quality` get the opposite treatment on purpose --
        those are measurements a caller may legitimately not have, so None
        preserves them. Carrying segments forward instead would pair one map's
        flat points with another map's boundaries.
        """
        session = self.mock_db.get_session()
        session.__enter__.return_value = session
        existing = BookAlignment(
            abs_id="book", alignment_map_json="[]",
            segments_json=json.dumps(SEGMENTS),
        )
        session.query.return_value.filter_by.return_value.first.return_value = existing

        service = AlignmentService(self.mock_db, Polisher())
        service._save_alignment("book", [{"char": 0, "ts": 0.0}], "lexical")

        self.assertIsNone(existing.segments_json)

    def test_ctc_overwriting_a_segmented_lexical_map_does_not_keep_its_segments(self):
        """The concrete corrupt-pairing route (issue #426).

        An out-of-order book stores segments from the lexical stage; the CTC
        upgrade then overwrites the map and passes no segments. If those
        survived, every lookup would apply the lexical map's boundaries to
        CTC's points.
        """
        session = self.mock_db.get_session()
        session.__enter__.return_value = session
        existing = BookAlignment(abs_id="book", alignment_map_json="[]")
        session.query.return_value.filter_by.return_value.first.return_value = existing
        service = AlignmentService(self.mock_db, Polisher())

        lexical_segments = [
            Segment(char_start=0, char_end=100, ts_start=500.0, ts_end=600.0,
                    inliers=40, residual=0.1),
            Segment(char_start=100, char_end=200, ts_start=0.0, ts_end=500.0,
                    inliers=40, residual=0.1),
        ]
        service._save_alignment("book", [{"char": 0, "ts": 500.0}], "lexical",
                                segments=lexical_segments)
        self.assertIsNotNone(existing.segments_json)

        # CTC upgrade: a different, denser map, and no segments.
        service._save_alignment("book", [{"char": 0, "ts": 0.0}], "ctc")

        self.assertIsNone(existing.segments_json,
                          "CTC's map must not inherit the lexical stage's segments")

    def test_save_alignment_writes_segments_when_supplied(self):
        session = self.mock_db.get_session()
        session.__enter__.return_value = session
        existing = BookAlignment(abs_id="book", alignment_map_json="[]")
        session.query.return_value.filter_by.return_value.first.return_value = existing

        service = AlignmentService(self.mock_db, Polisher())
        segments = [
            Segment(char_start=0, char_end=100, ts_start=100.0, ts_end=200.0,
                   inliers=42, residual=0.1),
            Segment(char_start=200, char_end=300, ts_start=0.0, ts_end=100.0,
                   inliers=37, residual=0.2),
        ]
        service._save_alignment("book", [{"char": 0, "ts": 0.0}], "lexical", segments=segments)

        stored = json.loads(existing.segments_json)
        self.assertEqual(stored, [
            {"char_start": 0, "char_end": 100, "ts_start": 100.0, "ts_end": 200.0},
            {"char_start": 200, "char_end": 300, "ts_start": 0.0, "ts_end": 100.0},
        ])
        # inliers/residual are fit diagnostics, not lookup data -- dropped.
        for segment in stored:
            self.assertNotIn("inliers", segment)
            self.assertNotIn("residual", segment)

    def test_save_alignment_new_row_stores_segments(self):
        session = self.mock_db.get_session()
        session.__enter__.return_value = session
        session.query.return_value.filter_by.return_value.first.return_value = None

        service = AlignmentService(self.mock_db, Polisher())
        segments = [Segment(char_start=0, char_end=10, ts_start=0.0, ts_end=1.0,
                           inliers=5, residual=0.01)]
        service._save_alignment("book", [{"char": 0, "ts": 0.0}], "lexical", segments=segments)

        stored = session.add.call_args[0][0]
        self.assertEqual(
            json.loads(stored.segments_json),
            [{"char_start": 0, "char_end": 10, "ts_start": 0.0, "ts_end": 1.0}],
        )


class TestSegmentsToJsonClamp(unittest.TestCase):
    """Issue #426 phase 3: a segment's fitted line can extrapolate below zero
    at its own `char_start` (real Four Past Midnight data: -175.1s, meaning
    the audio opens with ~175s of credits that have no matching ebook text).
    That is meaningful, but a negative audio timestamp must never be
    persisted. The clamp is asymmetric on purpose: it applies only at the
    `_segments_to_json` persistence boundary, never to the in-memory
    `Segment` itself, because `select_anchors` derives its own line from the
    segment's own two edges and clamping there would skew that line's slope.
    """

    def setUp(self):
        self.mock_db = MagicMock()

    def test_negative_ts_start_is_clamped_to_zero_on_persistence(self):
        session = self.mock_db.get_session()
        session.__enter__.return_value = session
        existing = BookAlignment(abs_id="book", alignment_map_json="[]")
        session.query.return_value.filter_by.return_value.first.return_value = existing

        service = AlignmentService(self.mock_db, Polisher())
        segment = Segment(char_start=0, char_end=1000, ts_start=-175.1, ts_end=200.0,
                          inliers=50, residual=1.0)
        service._save_alignment("book", [{"char": 0, "ts": 0.0}], "lexical",
                                segments=[segment])

        stored = json.loads(existing.segments_json)
        self.assertEqual(stored, [{"char_start": 0, "char_end": 1000,
                                   "ts_start": 0.0, "ts_end": 200.0}])

    def test_in_memory_segment_is_left_unclamped(self):
        """The asymmetry itself: persisting a negative `ts_start` must not
        mutate the `Segment` object the caller still holds (and, by
        extension, whatever line `select_anchors` derived from it earlier
        in the same call)."""
        segment = Segment(char_start=0, char_end=1000, ts_start=-175.1, ts_end=200.0,
                          inliers=50, residual=1.0)

        from src.services.alignment_service import _segments_to_json
        stored = json.loads(_segments_to_json([segment]))

        self.assertEqual(segment.ts_start, -175.1)
        self.assertEqual(stored[0]["ts_start"], 0.0)

    def test_non_negative_ts_start_is_unaffected(self):
        """Falsification guard: a healthy, already-non-negative `ts_start`
        must round-trip exactly, not just "not go negative"."""
        segment = Segment(char_start=0, char_end=1000, ts_start=42.5, ts_end=200.0,
                          inliers=50, residual=1.0)

        from src.services.alignment_service import _segments_to_json
        stored = json.loads(_segments_to_json([segment]))

        self.assertEqual(stored[0]["ts_start"], 42.5)


class TestSegmentsCacheInvalidation(unittest.TestCase):
    """`_get_segments` must be invalidated everywhere `_alignment_cache` is."""

    def test_save_alignment_invalidates_the_segments_cache(self):
        mock_db = MagicMock()
        _stub_row(mock_db, FLAT_MAP, SEGMENTS)
        service = AlignmentService(mock_db, Polisher())

        self.assertEqual(service._get_segments("book"), SEGMENTS)
        self.assertIn("book", service._segments_cache)

        mock_db.get_session().query.return_value.filter_by.return_value.first.return_value = None
        service._save_alignment("book", [{"char": 0, "ts": 0.0}], "lexical")

        self.assertNotIn("book", service._segments_cache)

    def test_restore_previous_alignment_invalidates_the_segments_cache(self):
        mock_db = MagicMock()
        session = mock_db.get_session()
        session.__enter__.return_value = session
        _stub_row(mock_db, FLAT_MAP, SEGMENTS)
        service = AlignmentService(mock_db, Polisher())
        service._get_segments("book")
        self.assertIn("book", service._segments_cache)

        backup = MagicMock()
        backup.align_method = "lexical"
        backup.alignment_map_json = json.dumps(FLAT_MAP)
        backup.total_chars = 300

        def query_side_effect(model):
            result = MagicMock()
            if model.__name__ == "BookAlignmentBackup":
                result.filter_by.return_value.first.return_value = backup
            else:
                entry = MagicMock()
                entry.alignment_map_json = json.dumps(FLAT_MAP)
                entry.segments_json = json.dumps(SEGMENTS)
                result.filter_by.return_value.first.return_value = entry
            return result

        session.query.side_effect = query_side_effect
        service.restore_previous_alignment("book")

        self.assertNotIn("book", service._segments_cache)


class TestBackupRestoreCarriesSegments(unittest.TestCase):
    """`segments_json` must travel with `alignment_map_json` through backup
    and restore. The two describe the same map; restoring one without the
    other pairs a map's flat points with a different map's segment
    boundaries and silently mis-resolves every lookup -- the corrupt-pairing
    hazard issue #426 phase 1 exists to prevent. Uses a real temp SQLite DB
    (mirrors `tests/test_ctc_acceptance_gate.py`) since the bug is about what
    actually lands in two real rows, not a mock's call arguments."""

    def setUp(self):
        from src.db.database_service import DatabaseService

        self.temp_dir = tempfile.mkdtemp()
        self.db_path = str(Path(self.temp_dir) / "backup_restore.db")
        self.db_service = DatabaseService(self.db_path)
        self.service = AlignmentService(self.db_service, Polisher())

    def tearDown(self):
        self.db_service.db_manager.close()
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _live_row(self, abs_id):
        with self.db_service.get_session() as session:
            row = session.query(BookAlignment).filter_by(abs_id=abs_id).first()
            return (row.alignment_map_json, row.segments_json) if row else (None, None)

    def _backup_row(self, abs_id):
        with self.db_service.get_session() as session:
            row = session.query(BookAlignmentBackup).filter_by(abs_id=abs_id).first()
            return (row.alignment_map_json, row.segments_json) if row else (None, None)

    def test_backup_then_restore_of_a_segmented_map_round_trips_segments(self):
        """Segments travel with the map both into the backup and back out,
        surviving a second segmented write in between."""
        v1_segments = [Segment(**s, inliers=1, residual=0.0) for s in SEGMENTS]
        self.service._save_alignment("book", FLAT_MAP, "lexical", total_chars=300,
                                     segments=v1_segments)
        self.assertTrue(self.service._backup_alignment("book"))

        backup_map, backup_segments = self._backup_row("book")
        self.assertEqual(json.loads(backup_map), FLAT_MAP)
        self.assertEqual(json.loads(backup_segments), SEGMENTS)

        # Overwrite the live row with a second, different segmented map --
        # what a Phase 2 re-align would do.
        v2_map = [{"char": 0, "ts": 0.0}, {"char": 300, "ts": 300.0}]
        v2_segments = [Segment(char_start=0, char_end=300, ts_start=0.0, ts_end=300.0,
                               inliers=1, residual=0.0)]
        self.service._save_alignment("book", v2_map, "storyteller", total_chars=300,
                                     segments=v2_segments)

        self.assertTrue(self.service.restore_previous_alignment("book"))

        map_json, segments_json = self._live_row("book")
        self.assertEqual(json.loads(map_json), FLAT_MAP)
        self.assertEqual(json.loads(segments_json), SEGMENTS)

    def test_backup_alignment_update_branch_also_carries_segments(self):
        """`_backup_alignment`'s update-an-existing-backup branch (not just
        its create-a-new-backup branch) must carry `segments_json` too."""
        self.service._save_alignment("book", FLAT_MAP, "lexical", total_chars=300,
                                     segments=[Segment(**s, inliers=1, residual=0.0)
                                               for s in SEGMENTS])
        self.assertTrue(self.service._backup_alignment("book"))  # create branch

        v2_segments = [Segment(char_start=0, char_end=300, ts_start=0.0, ts_end=300.0,
                               inliers=1, residual=0.0)]
        self.service._save_alignment("book", [{"char": 0, "ts": 0.0}], "storyteller",
                                     total_chars=300, segments=v2_segments)
        self.assertTrue(self.service._backup_alignment("book"))  # update branch

        _, backup_segments = self._backup_row("book")
        self.assertEqual(json.loads(backup_segments), [
            {"char_start": 0, "char_end": 300, "ts_start": 0.0, "ts_end": 300.0},
        ])

    def test_restore_creates_a_new_row_with_segments_when_none_existed(self):
        """The `session.add(...)` branch of restore (no live row yet) also
        carries `segments_json` from the backup."""
        with self.db_service.get_session() as session:
            session.add(BookAlignmentBackup(
                abs_id="book", alignment_map_json=json.dumps(FLAT_MAP),
                align_method="lexical", total_chars=300,
                segments_json=json.dumps(SEGMENTS),
            ))

        self.assertTrue(self.service.restore_previous_alignment("book"))

        map_json, segments_json = self._live_row("book")
        self.assertEqual(json.loads(map_json), FLAT_MAP)
        self.assertEqual(json.loads(segments_json), SEGMENTS)

    def test_restore_does_not_mix_a_segmented_map_with_a_different_backups_segments(self):
        """THE corrupt-pairing regression test (the point of this task). The
        live row has a segmented map (M_new/S_new). Its backup holds a
        DIFFERENT, non-segmented map (M_old, segments_json NULL) -- exactly
        what a pre-Phase-2 backup looks like once the live row has since
        been upgraded to a segmented map. Restoring must land the map and
        its segments together: `segments_json` afterwards must be the
        backup's value (NULL here), never the pre-restore (S_new) value --
        that mismatch is silent position corruption with no error and no
        log line."""
        m_old = [{"char": 0, "ts": 0.0}, {"char": 300, "ts": 30.0}]
        m_new = FLAT_MAP
        s_new = SEGMENTS

        with self.db_service.get_session() as session:
            session.add(BookAlignment(
                abs_id="book", alignment_map_json=json.dumps(m_new),
                align_method="storyteller", total_chars=300,
                segments_json=json.dumps(s_new),
            ))
            session.add(BookAlignmentBackup(
                abs_id="book", alignment_map_json=json.dumps(m_old),
                align_method="lexical", total_chars=300,
                segments_json=None,
            ))

        self.assertTrue(self.service.restore_previous_alignment("book"))

        map_json, segments_json = self._live_row("book")
        self.assertEqual(json.loads(map_json), m_old)
        # The bug this test exists to catch: leaving segments_json untouched
        # would leave it as s_new's JSON here instead of None.
        self.assertIsNone(segments_json)


class TestSegmentsMigrationAppliesToHead(unittest.TestCase):
    """The `segments_json` migration applies base -> head on a fresh temp
    SQLite, additively (mirrors tests/test_alignment_quality_migration.py)."""

    def setUp(self):
        from src.db.database_service import DatabaseService

        self.temp_dir = tempfile.mkdtemp()
        self.db_path = str(Path(self.temp_dir) / "segments_migration.db")
        self.db_service = DatabaseService(self.db_path)

    def tearDown(self):
        self.db_service.db_manager.close()
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_segments_json_column_exists_and_is_nullable(self):
        inspector = sa.inspect(self.db_service.db_manager.engine)
        columns = {c["name"]: c for c in inspector.get_columns("book_alignments")}
        self.assertIn("segments_json", columns)
        self.assertTrue(columns["segments_json"]["nullable"])

    def test_backup_table_also_gets_segments_json_column(self):
        """The backup table must carry the identical column -- a map and its
        segments travel together, so the backup row needs somewhere to hold
        the segments belonging to the map it backed up."""
        inspector = sa.inspect(self.db_service.db_manager.engine)
        columns = {c["name"]: c for c in inspector.get_columns("book_alignment_backups")}
        self.assertIn("segments_json", columns)
        self.assertTrue(columns["segments_json"]["nullable"])

    def test_orm_model_matches_the_migrated_schema(self):
        """A model that drifts from the migration is exactly the class of
        bug this catches: a freshly created DB (`Base.metadata.create_all`)
        and a migrated one would silently disagree on column shape. Checked
        for both tables the migration touches."""
        from src.db.models import Base

        migrated = sa.inspect(self.db_service.db_manager.engine)

        model_engine = sa.create_engine("sqlite:///:memory:")
        try:
            Base.metadata.create_all(model_engine)
            built = sa.inspect(model_engine)
            for table in ("book_alignments", "book_alignment_backups"):
                mig_cols = {c["name"]: str(c["type"]) for c in migrated.get_columns(table)}
                built_cols = {c["name"]: str(c["type"]) for c in built.get_columns(table)}
                self.assertEqual(mig_cols.get("segments_json"), built_cols.get("segments_json"))
        finally:
            model_engine.dispose()


class TestSegmentsMigrationModuleDirectly(unittest.TestCase):
    """Exercises the migration's own upgrade/downgrade against pre-existing
    `book_alignments` and `book_alignment_backups` tables, isolated from the
    rest of the chain (mirrors tests/test_alignment_quality_migration.py's
    direct-module pattern)."""

    def setUp(self):
        import importlib.util

        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        migration_path = (
            Path(__file__).resolve().parents[1]
            / "alembic/versions/a6c3e9f1b7d4_add_alignment_segments.py"
        )
        spec = importlib.util.spec_from_file_location("segments_migration", migration_path)
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)

        self.engine = sa.create_engine("sqlite:///:memory:")
        with self.engine.begin() as conn:
            conn.execute(sa.text(
                "CREATE TABLE book_alignments ("
                "abs_id VARCHAR(255) PRIMARY KEY, "
                "alignment_map_json TEXT NOT NULL, "
                "align_method VARCHAR(32), "
                "total_chars INTEGER, "
                "quality_score FLOAT, "
                "quality_detail TEXT)"
            ))
            conn.execute(sa.text(
                "CREATE TABLE book_alignment_backups ("
                "abs_id VARCHAR(255) PRIMARY KEY, "
                "alignment_map_json TEXT NOT NULL, "
                "align_method VARCHAR(32), "
                "total_chars INTEGER, "
                "backed_up_at DATETIME)"
            ))
        self._MigrationContext = MigrationContext
        self._Operations = Operations

    def tearDown(self):
        self.engine.dispose()

    def _run(self, fn):
        with self.engine.begin() as conn:
            ctx = self._MigrationContext.configure(conn)
            operations = self._Operations(ctx)
            old_op = self.mod.op
            try:
                self.mod.op = operations
                fn()
            finally:
                self.mod.op = old_op

    def _columns(self, table="book_alignments"):
        return {c["name"]: c for c in sa.inspect(self.engine).get_columns(table)}

    def test_upgrade_adds_the_nullable_column(self):
        self._run(self.mod.upgrade)
        for table in ("book_alignments", "book_alignment_backups"):
            columns = self._columns(table)
            self.assertIn("segments_json", columns)
            self.assertTrue(columns["segments_json"]["nullable"])

    def test_upgrade_is_idempotent(self):
        self._run(self.mod.upgrade)
        self._run(self.mod.upgrade)
        for table in ("book_alignments", "book_alignment_backups"):
            self.assertIn("segments_json", self._columns(table))

    def test_downgrade_drops_the_column(self):
        self._run(self.mod.upgrade)
        self._run(self.mod.downgrade)
        for table in ("book_alignments", "book_alignment_backups"):
            self.assertNotIn("segments_json", self._columns(table))

    def test_downgrade_is_idempotent(self):
        self._run(self.mod.upgrade)
        self._run(self.mod.downgrade)
        self._run(self.mod.downgrade)
        for table in ("book_alignments", "book_alignment_backups"):
            self.assertNotIn("segments_json", self._columns(table))


if __name__ == "__main__":
    unittest.main()
