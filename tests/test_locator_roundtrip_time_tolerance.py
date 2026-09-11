"""Regression tests: judge a locator round-trip in SECONDS on the audio
timeline whenever an alignment map exists, instead of in characters.

Characters are the wrong unit once a book has a SEGMENTED alignment map
(out-of-order narration, issue #426): the char->time function is
discontinuous at segment seams, so a tiny char round-trip error can land on
the far side of a seam and be hours off in audio time, while a large char
error on the near side of the same seam is negligible. Measured on the real
"Tress of the Emerald Sea" seam: a 2-char offset gap is a 12.40-hour (44640s)
audio-time gap.

Covers:
1. `AlignmentService.get_time_for_char` == `get_time_for_text(char_offset_hint=X)`
   for the same offset (pins the extraction as behavior-preserving).
2. `SyncManager._roundtrip_time_error` returns None with no alignment
   service, and None when the map lookup itself returns None.
3. The headline regression: a 2-char offset difference that maps to a
   12.40-hour time difference is REJECTED by `_validate_and_stabilize_locator`
   even though 2 chars is within the default character tolerance.
4. The real observed snap (115 chars / ~8s) is still ACCEPTED.
5. A large char difference with a small time difference is ACCEPTED (the
   seam seen from the other side).
6. With no alignment map, `_validate_and_stabilize_locator` behaves exactly
   like today's character comparison — both the accept and reject case.
7. `_hydrate_cfi_locator`, by contrast, stays CHARACTER-judged: it accepts a
   character-close CFI even across the same 12.40-hour seam, and still
   rejects a character-far one. That locator reaches only ebook readers, none
   of which re-derive an audio timestamp from it, so seconds get no veto
   there — and refusing would hand the device a percentage it ignores (#364).
"""

import os
import unittest
from unittest.mock import MagicMock, patch

from src.db.models import Book
from src.services.alignment_service import AlignmentService
from src.sync_clients.sync_client_interface import LocatorResult
from src.sync_manager import SyncManager
from src.utils.polisher import Polisher

# The real measured "Tress of the Emerald Sea" seam (see CLAUDE.md / issue
# #426 follow-up): a 2-char gap at a segment seam is a 12.40-hour audio-time
# gap. Built from two segments whose time ranges do not neighbour each other,
# with map points placed so each of the two queried char offsets clamps to a
# different one of those segments' edges (see
# `AlignmentService._nearest_segment_edge_ts` / the seam mechanics in
# `tests/test_segmented_map.py`).
SEAM_SEGMENTS = [
    {"char_start": 0, "char_end": 100, "ts_start": 0.0, "ts_end": 100.0},
    {"char_start": 100, "char_end": 10000, "ts_start": 44740.0, "ts_end": 50000.0},
]
SEAM_MAP = [
    {"char": 0, "ts": 0.0},
    {"char": 50, "ts": 50.0},
    {"char": 99, "ts": 99.0},
    {"char": 100, "ts": 44740.0},
    {"char": 101, "ts": 44741.0},
    {"char": 10000, "ts": 50000.0},
]
SEAM_OFFSET_A = 99   # last char of the early segment -> clamps to its ts_end
SEAM_OFFSET_B = 101  # just inside the late segment -> clamps to its ts_start
SEAM_TIME_ERROR_SECONDS = 44640.0  # 12.40 hours, exactly the measured Tress gap

# The real observed snap from production logs: "Hydrated missing CFI for
# leader 'KoSync' at offset 223420 (roundtrip=223305)" — a 115-char
# round-trip error that is ~8 seconds on that book's audio timeline, and
# must keep being accepted.
OBSERVED_SNAP_TARGET_OFFSET = 223420
OBSERVED_SNAP_ROUNDTRIP_OFFSET = 223305
OBSERVED_SNAP_TIME_ERROR_SECONDS = 8.0


def _stub_alignment_row(mock_db, alignment_map, segments=None):
    """Wire a MagicMock DatabaseService to return one BookAlignment-shaped
    row for every abs_id (mirrors `_stub_row` in tests/test_segmented_map.py)."""
    import json

    session = mock_db.get_session()
    session.__enter__.return_value = session
    entry = MagicMock()
    entry.alignment_map_json = json.dumps(alignment_map)
    entry.segments_json = json.dumps(segments) if segments is not None else None
    session.query.return_value.filter_by.return_value.first.return_value = entry
    return session


def _make_manager(ebook_parser=None, alignment_service=None):
    """Build a SyncManager with mocked dependencies (mirrors
    tests/test_sync_manager_stabilizer.py's `_make_manager`)."""
    db = MagicMock()
    db.get_books_by_status.return_value = []
    parser = ebook_parser or MagicMock()
    parser.locator_roundtrip_tolerance = 2

    return SyncManager(
        abs_client=MagicMock(),
        booklore_client=MagicMock(),
        hardcover_client=MagicMock(),
        transcriber=MagicMock(),
        ebook_parser=parser,
        database_service=db,
        storyteller_client=MagicMock(),
        sync_clients={},
        alignment_service=alignment_service,
        library_service=None,
        migration_service=None,
        epub_cache_dir="/tmp/epub_cache",
        data_dir="/tmp",
        books_dir="/tmp/books",
    )


def _make_book(abs_id="test-book", ebook_filename="test.epub"):
    return Book(
        abs_id=abs_id,
        abs_title="Test Book",
        ebook_filename=ebook_filename,
        status="active",
    )


class TestGetTimeForCharExtraction(unittest.TestCase):
    """Coverage 1: `get_time_for_char` is the same lookup `get_time_for_text`
    delegates to when given a char offset hint — not a second, drifting
    implementation."""

    def setUp(self):
        self.mock_db = MagicMock()
        _stub_alignment_row(self.mock_db, [{"char": 0, "ts": 0.0}, {"char": 100, "ts": 100.0}])
        self.service = AlignmentService(self.mock_db, Polisher())

    def test_get_time_for_char_matches_get_time_for_text_hint_path(self):
        direct = self.service.get_time_for_char("book", 50)
        via_hint = self.service.get_time_for_text("book", "irrelevant query text", char_offset_hint=50)
        self.assertEqual(direct, via_hint)
        self.assertEqual(direct, 50.0)

    def test_get_time_for_text_without_hint_still_returns_none(self):
        """Unrelated to the extraction, but pins that the pre-existing
        no-hint behavior of get_time_for_text is untouched by the delegation."""
        self.assertIsNone(self.service.get_time_for_text("book", "some text"))


class TestRoundtripTimeErrorNoneCases(unittest.TestCase):
    """Coverage 2: `_roundtrip_time_error` returns None — "cannot judge in
    time" — whenever it cannot compute a real time delta."""

    def test_none_with_no_alignment_service(self):
        manager = _make_manager(alignment_service=None)
        self.assertIsNone(manager._roundtrip_time_error("book", 10, 20))

    def test_none_when_the_map_lookup_returns_none(self):
        alignment_service = MagicMock()
        alignment_service.get_time_for_char.return_value = None
        manager = _make_manager(alignment_service=alignment_service)

        self.assertIsNone(manager._roundtrip_time_error("book", 10, 20))

    def test_none_when_the_lookup_raises(self):
        """Neither guard may raise — an exception from the alignment service
        must degrade to 'cannot judge in time', not propagate."""
        alignment_service = MagicMock()
        alignment_service.get_time_for_char.side_effect = RuntimeError("boom")
        manager = _make_manager(alignment_service=alignment_service)

        self.assertIsNone(manager._roundtrip_time_error("book", 10, 20))

    def test_computes_the_absolute_time_delta_when_both_lookups_succeed(self):
        alignment_service = MagicMock()
        alignment_service.get_time_for_char.side_effect = lambda abs_id, offset: {
            10: 100.0, 20: 108.0,
        }[offset]
        manager = _make_manager(alignment_service=alignment_service)

        self.assertEqual(manager._roundtrip_time_error("book", 10, 20), 8.0)
        self.assertEqual(manager._roundtrip_time_error("book", 20, 10), 8.0)


class TestValidateAndStabilizeLocatorHeadlineRegression(unittest.TestCase):
    """Coverage 3 & 4 & 5: `_validate_and_stabilize_locator`'s xpath
    round-trip check, judged in audio-time via a mocked alignment service."""

    def setUp(self):
        os.environ.pop("LOCATOR_ROUNDTRIP_TOLERANCE_SECONDS", None)
        os.environ.pop("CROSSFORMAT_ROUNDTRIP_TOLERANCE_CHARS", None)

    def tearDown(self):
        os.environ.pop("LOCATOR_ROUNDTRIP_TOLERANCE_SECONDS", None)
        os.environ.pop("CROSSFORMAT_ROUNDTRIP_TOLERANCE_CHARS", None)

    def _build(self, time_lookup):
        alignment_service = MagicMock()
        alignment_service.get_time_for_char.side_effect = (
            lambda abs_id, offset: time_lookup[offset]
        )
        parser = MagicMock()
        parser.locator_roundtrip_tolerance = 2
        parser.get_sentence_level_ko_xpath.return_value = None  # no sentence fallback
        manager = _make_manager(ebook_parser=parser, alignment_service=alignment_service)
        book = _make_book()
        return manager, parser, book

    def test_2char_gap_mapping_to_12_hours_is_rejected(self):
        """THE headline regression test, built from the measured Tress numbers:
        a 2-char round-trip error (inside the default 2-char tolerance) must
        still be rejected once it implies a 12.40-hour audio-time gap."""
        manager, parser, book = self._build({
            SEAM_OFFSET_A: 100.0,      # target_offset's own time
            SEAM_OFFSET_B: 44740.0,    # the resolved xpath's time
        })
        parser.resolve_xpath_to_index.return_value = SEAM_OFFSET_B  # ko_offset
        locator = LocatorResult(
            percentage=0.01,
            xpath="/body/p[1]/text().0",
            perfect_ko_xpath="/body/p[1]/text().0",
            match_index=SEAM_OFFSET_A,
        )

        # Sanity: the raw character delta really is inside the char tolerance
        # the old guard used exclusively.
        self.assertLessEqual(abs(SEAM_OFFSET_B - SEAM_OFFSET_A), parser.locator_roundtrip_tolerance)

        with self.assertLogs("src.sync_manager", level="INFO") as logs:
            result = manager._validate_and_stabilize_locator(
                book, target_offset=SEAM_OFFSET_A, locator=locator, ebook_filename="test.epub",
            )

        self.assertIsNotNone(result)
        # Rejected -> xpath removed, not kept just because chars were close.
        self.assertIsNone(result.xpath)
        self.assertIsNone(result.perfect_ko_xpath)
        self.assertTrue(
            any("Locator round-trip rejected" in message and "44640.0s" in message
                for message in logs.output),
            f"expected an INFO rejection log naming the time error, got: {logs.output}",
        )

    def test_char_far_but_time_close_is_still_rejected(self):
        """Audio time must never LICENSE a character-far locator.

        This locator is written to ebook clients, so the reader's eye lands at a
        text position: 9999 characters away is wrong for the reader no matter how
        close it happens to sit on the audio timeline. Time is a veto only.
        """
        target_offset = 0
        resolved_offset = 9999
        manager, parser, book = self._build({
            target_offset: 0.0,
            resolved_offset: 5.0,          # only 5s apart in audio
        })
        parser.resolve_xpath_to_index.return_value = resolved_offset
        parser.get_sentence_level_ko_xpath.return_value = None
        locator = LocatorResult(
            percentage=0.0,
            xpath="/body/p[1]/text().0",
            perfect_ko_xpath="/body/p[1]/text().0",
            match_index=target_offset,
        )

        self.assertGreater(abs(resolved_offset - target_offset), parser.locator_roundtrip_tolerance)

        result = manager._validate_and_stabilize_locator(
            book, target_offset=target_offset, locator=locator, ebook_filename="test.epub",
        )

        self.assertIsNotNone(result)
        # Degraded to percent-only rather than kept: chars decide acceptance.
        self.assertIsNone(result.xpath)
        self.assertIsNone(result.perfect_ko_xpath)

    def test_char_close_and_time_close_is_accepted(self):
        """The ordinary case: within char tolerance and within seconds tolerance."""
        target_offset = 10000
        resolved_offset = 10001            # 1 char, inside the 2-char default
        manager, parser, book = self._build({
            target_offset: 500.0,
            resolved_offset: 500.4,
        })
        parser.resolve_xpath_to_index.return_value = resolved_offset
        locator = LocatorResult(
            percentage=0.5,
            xpath="/body/p[500]/text().0",
            perfect_ko_xpath="/body/p[500]/text().0",
            match_index=target_offset,
        )

        result = manager._validate_and_stabilize_locator(
            book, target_offset=target_offset, locator=locator, ebook_filename="test.epub",
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.xpath, "/body/p[500]/text().0")
        self.assertEqual(result.perfect_ko_xpath, "/body/p[500]/text().0")


class TestValidateAndStabilizeLocatorNoMapUnchanged(unittest.TestCase):
    """Coverage 6: with no alignment map, behavior is exactly today's
    character comparison — both the accept and reject case."""

    def setUp(self):
        self.parser = MagicMock()
        self.parser.locator_roundtrip_tolerance = 2
        self.parser.get_sentence_level_ko_xpath.return_value = None
        self.manager = _make_manager(ebook_parser=self.parser, alignment_service=None)
        self.book = _make_book()

    def test_accepts_within_character_tolerance(self):
        self.parser.resolve_xpath_to_index.return_value = 100  # matches target exactly
        locator = LocatorResult(
            percentage=0.5,
            xpath="/body/p[1]/text().0",
            perfect_ko_xpath="/body/p[1]/text().0",
            match_index=100,
        )

        result = self.manager._validate_and_stabilize_locator(
            self.book, target_offset=100, locator=locator, ebook_filename="test.epub",
        )

        self.assertEqual(result.xpath, "/body/p[1]/text().0")

    def test_rejects_outside_character_tolerance(self):
        self.parser.resolve_xpath_to_index.return_value = 250  # 150 chars off, way outside tolerance=2
        locator = LocatorResult(
            percentage=0.5,
            xpath="/body/p[1]/text().0",
            perfect_ko_xpath="/body/p[1]/text().0",
            match_index=100,
        )

        result = self.manager._validate_and_stabilize_locator(
            self.book, target_offset=100, locator=locator, ebook_filename="test.epub",
        )

        self.assertIsNone(result.xpath)
        self.assertIsNone(result.perfect_ko_xpath)


class TestRoundtripSecondsToleranceSettingIsFailSafe(unittest.TestCase):
    """Coverage 8: clearing LOCATOR_ROUNDTRIP_TOLERANCE_SECONDS in the settings
    UI must not disable the locator path.

    A cleared settings field is persisted as "" and mirrored verbatim into
    os.environ (DB values always win), so the raw `float(os.environ.get(K, 30))`
    this replaced raised ValueError. Both callers sit inside a broad `except
    Exception`, so nothing crashed — `_resolve_alignment_locator_from_abs_timestamp`
    simply returned "no locator" for every book, for as long as the field
    stayed empty."""

    def setUp(self):
        self._saved = os.environ.get("LOCATOR_ROUNDTRIP_TOLERANCE_SECONDS")

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("LOCATOR_ROUNDTRIP_TOLERANCE_SECONDS", None)
        else:
            os.environ["LOCATOR_ROUNDTRIP_TOLERANCE_SECONDS"] = self._saved

    def test_cleared_blank_and_garbage_values_all_fall_back_to_the_default(self):
        for raw in ("", "   ", "abc", None):
            with self.subTest(value=raw):
                if raw is None:
                    os.environ.pop("LOCATOR_ROUNDTRIP_TOLERANCE_SECONDS", None)
                else:
                    os.environ["LOCATOR_ROUNDTRIP_TOLERANCE_SECONDS"] = raw
                self.assertEqual(SyncManager._locator_roundtrip_seconds_tolerance(), 30.0)

    def test_a_real_value_is_honoured(self):
        os.environ["LOCATOR_ROUNDTRIP_TOLERANCE_SECONDS"] = "45"
        self.assertEqual(SyncManager._locator_roundtrip_seconds_tolerance(), 45.0)

    def test_a_cleared_setting_still_validates_a_locator(self):
        """The end-to-end shape of the bug: with the field cleared, a locator
        that round-trips exactly still comes back intact."""
        os.environ["LOCATOR_ROUNDTRIP_TOLERANCE_SECONDS"] = ""
        parser = MagicMock()
        parser.locator_roundtrip_tolerance = 2
        parser.get_sentence_level_ko_xpath.return_value = None
        parser.resolve_xpath_to_index.return_value = 100
        manager = _make_manager(ebook_parser=parser, alignment_service=None)

        result = manager._validate_and_stabilize_locator(
            _make_book(),
            target_offset=100,
            locator=LocatorResult(
                percentage=0.5,
                xpath="/body/p[1]/text().0",
                perfect_ko_xpath="/body/p[1]/text().0",
                match_index=100,
            ),
            ebook_filename="test.epub",
        )

        self.assertEqual(result.xpath, "/body/p[1]/text().0")


class TestHydrateCfiLocatorIsCharacterJudged(unittest.TestCase):
    """Coverage 7: `_hydrate_cfi_locator` judges its round-trip in CHARACTERS,
    deliberately — audio time gets no veto on this path.

    The locator this builds reaches only `_CFI_DEPENDENT_CLIENTS` (ABSEbook,
    BookOrbit, Grimmory, CWA). Every one is an ebook reader that navigates by
    text position, and none re-derives an audio timestamp from it, so audio
    time has no standing to refuse it — while refusing costs the bare
    percentage the device ignores (#364). A seconds veto lived here briefly
    and was removed; these tests exist so it is not reintroduced by reflex."""

    def setUp(self):
        os.environ.pop("LOCATOR_ROUNDTRIP_TOLERANCE_SECONDS", None)
        self.total_len = 10000  # 1% of book == 100 chars
        self.target_offset = SEAM_OFFSET_A    # 99
        self.roundtrip_offset = SEAM_OFFSET_B  # 101 -> 2 chars away, 0.02% of book

    def tearDown(self):
        os.environ.pop("LOCATOR_ROUNDTRIP_TOLERANCE_SECONDS", None)

    def _build_manager(self, time_lookup, roundtrip_offset=None):
        alignment_service = MagicMock()
        alignment_service.get_time_for_char.side_effect = (
            lambda abs_id, offset: time_lookup.get(offset)
        )
        manager = _make_manager(alignment_service=alignment_service)
        manager._get_cached_ebook_text = MagicMock(return_value=("x" * self.total_len, self.total_len))
        manager.ebook_parser.get_locator_from_char_offset.return_value = LocatorResult(percentage=0.0, cfi="/6/4!")
        manager.ebook_parser.resolve_cfi_to_index.return_value = (
            self.roundtrip_offset if roundtrip_offset is None else roundtrip_offset
        )
        return manager

    def test_character_close_is_accepted_even_across_a_12_hour_seam(self):
        """The headline case for this path: 2 chars apart, 12.40 HOURS apart in
        audio. The reader's eye lands at a text position, so this is the right
        CFI for them; refusing it would hand the device a percentage it drops."""
        early_ts, late_ts = 100.0, 100.0 + SEAM_TIME_ERROR_SECONDS
        manager = self._build_manager({
            self.target_offset: early_ts,
            self.roundtrip_offset: late_ts,
        })
        locator = LocatorResult(percentage=self.target_offset / self.total_len, match_index=self.target_offset)

        # The time gap really is the full measured Tress seam (12.40 hours).
        self.assertEqual(abs(late_ts - early_ts), SEAM_TIME_ERROR_SECONDS)

        result = manager._hydrate_cfi_locator(
            locator, "book.epub", "abs-1", "Test Book", "KoSync", 0.0099, lambda p: f"{p*100:.2f}%",
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.cfi, "/6/4!")

    def test_character_far_is_still_rejected(self):
        """The 1%-of-book character bound is intact and is what decides here."""
        far_offset = self.target_offset + 500  # 5% of the book
        manager = self._build_manager(
            {self.target_offset: 100.0, far_offset: 101.0},  # 1s apart in audio
            roundtrip_offset=far_offset,
        )
        locator = LocatorResult(percentage=self.target_offset / self.total_len, match_index=self.target_offset)

        result = manager._hydrate_cfi_locator(
            locator, "book.epub", "abs-1", "Test Book", "KoSync", 0.0099, lambda p: f"{p*100:.2f}%",
        )

        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
