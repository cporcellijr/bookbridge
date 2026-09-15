"""Issue #426 phase 4: the alignment-health query is widened by score, and
unscored legacy maps are backfilled lazily.

The load-bearing test in this file is
`test_low_score_lexical_map_is_included_and_high_score_lexical_map_is_excluded`:
before this phase, `get_books_needing_llm_realign()` looked only at
`align_method` (NULL / linear / storyteller_linear), so a badly broken
'lexical' map (Immortal Mana, Starfish, Bestial, Four Past Midnight — all
measured live, issue #426) was reported healthy.
"""

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from typing import Dict, List

from src.db.database_service import DatabaseService
from src.db.models import Book, BookAlignment
from src.services.alignment_service import AlignmentService
from src.services.map_quality import ALIGNMENT_QUALITY_REALIGN_THRESHOLD, score_map
from src.utils.polisher import Polisher


def _dense_map(length: int, step: int) -> List[Dict]:
    """An evenly-paced, densely-anchored synthetic map: scores near 1.0."""
    return [{"char": c, "ts": round(c * 0.01, 3)} for c in range(0, length + 1, step)]


def _broken_map(length: int) -> List[Dict]:
    """A sparse, two-point map: scores far below the realign threshold."""
    return [{"char": 0, "ts": 0.0}, {"char": length, "ts": float(length)}]


class _AlignmentQualityTestBase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db = DatabaseService(str(Path(self.temp_dir) / "quality_backfill.db"))

    def tearDown(self):
        self.db.db_manager.close()
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _save_scored(self, abs_id: str, alignment_map, align_method: str,
                      total_chars: int, quality=None) -> None:
        """Write a BookAlignment row directly (mirrors
        AlignmentService._save_alignment's upsert shape) with an optional
        pre-computed quality score."""
        with self.db.get_session() as session:
            session.add(BookAlignment(
                abs_id=abs_id,
                alignment_map_json=json.dumps(alignment_map),
                align_method=align_method,
                total_chars=total_chars,
                quality_score=quality.score if quality is not None else None,
                quality_detail=None,
            ))


class TestGetBooksNeedingLlmRealignQualityThreshold(_AlignmentQualityTestBase):

    def test_low_score_lexical_map_is_included_and_high_score_lexical_map_is_excluded(self):
        good = _dense_map(1000, 10)
        good_quality = score_map(good)
        self.assertGreaterEqual(good_quality.score, ALIGNMENT_QUALITY_REALIGN_THRESHOLD)

        bad = _broken_map(1000)
        bad_quality = score_map(bad)
        self.assertLess(bad_quality.score, ALIGNMENT_QUALITY_REALIGN_THRESHOLD)

        self._save_scored("good-book", good, "lexical", 1000, quality=good_quality)
        self._save_scored("bad-book", bad, "lexical", 1000, quality=bad_quality)

        targets = self.db.get_books_needing_llm_realign()

        self.assertIn("bad-book", targets)
        self.assertNotIn("good-book", targets)

    def test_unscored_lexical_map_is_not_flagged_by_score_alone(self):
        """A NULL quality_score (map stored before scoring existed) must not, on
        its own, mark a 'lexical' map for realign -- only a recorded low score
        does. `align_method` NULL/linear/storyteller_linear still flags it."""
        unscored = _dense_map(1000, 10)
        self._save_scored("unscored-lexical", unscored, "lexical", 1000, quality=None)

        targets = self.db.get_books_needing_llm_realign()

        self.assertNotIn("unscored-lexical", targets)

    def test_null_align_method_still_flagged_regardless_of_score(self):
        good = _dense_map(1000, 10)
        good_quality = score_map(good)
        self._save_scored("pre-llm", good, None, 1000, quality=good_quality)

        self.assertIn("pre-llm", self.db.get_books_needing_llm_realign())

    def test_linear_method_still_flagged_regardless_of_score(self):
        good = _dense_map(1000, 10)
        good_quality = score_map(good)
        self._save_scored("linear-book", good, "linear", 1000, quality=good_quality)

        self.assertIn("linear-book", self.db.get_books_needing_llm_realign())


class TestGetAlignmentProvenanceQualityScore(_AlignmentQualityTestBase):

    def test_low_score_lexical_row_is_reported_with_its_score(self):
        bad = _broken_map(1000)
        bad_quality = score_map(bad)
        self.db.save_book(Book(abs_id="bad-book", abs_title="Bad Book"))
        self._save_scored("bad-book", bad, "lexical", 1000, quality=bad_quality)

        provenance = self.db.get_alignment_provenance()

        row = next(b for b in provenance["books"] if b["abs_id"] == "bad-book")
        self.assertEqual(row["quality_score"], bad_quality.score)
        self.assertTrue(row["needs_realign"])

    def test_high_score_lexical_row_is_not_reported(self):
        good = _dense_map(1000, 10)
        good_quality = score_map(good)
        self.db.save_book(Book(abs_id="good-book", abs_title="Good Book"))
        self._save_scored("good-book", good, "lexical", 1000, quality=good_quality)

        provenance = self.db.get_alignment_provenance()

        self.assertNotIn("good-book", {b["abs_id"] for b in provenance["books"]})

    def test_never_selects_the_alignment_map_blob(self):
        """Guards the existing contract this docstring promises: the report must
        never pull the (potentially 10-15MB) alignment_map_json column."""
        bad = _broken_map(1000)
        self._save_scored("bad-book", bad, "lexical", 1000, quality=score_map(bad))

        provenance = self.db.get_alignment_provenance()

        row = next(b for b in provenance["books"] if b["abs_id"] == "bad-book")
        self.assertNotIn("alignment_map_json", row)


class TestBackfillAlignmentQuality(_AlignmentQualityTestBase):

    def test_scores_only_unscored_maps(self):
        already_scored = _dense_map(1000, 10)
        already_quality = score_map(already_scored)
        self._save_scored("scored-book", already_scored, "lexical", 1000, quality=already_quality)

        unscored = _broken_map(1000)
        self._save_scored("unscored-book", unscored, "lexical", 1000, quality=None)

        scored_count = self.db.backfill_alignment_quality()

        self.assertEqual(scored_count, 1)
        with self.db.get_session() as session:
            row = session.query(BookAlignment).filter_by(abs_id="unscored-book").first()
            self.assertIsNotNone(row.quality_score)
            self.assertIsNotNone(row.quality_detail)
            unchanged = session.query(BookAlignment).filter_by(abs_id="scored-book").first()
            self.assertEqual(unchanged.quality_score, already_quality.score)

    def test_respects_limit(self):
        for i in range(5):
            self._save_scored(f"book-{i}", _broken_map(100), "lexical", 100, quality=None)

        scored_count = self.db.backfill_alignment_quality(limit=2)

        self.assertEqual(scored_count, 2)
        with self.db.get_session() as session:
            remaining_unscored = (
                session.query(BookAlignment)
                .filter(BookAlignment.quality_score.is_(None))
                .count()
            )
            self.assertEqual(remaining_unscored, 3)

    def test_is_idempotent_on_a_second_call(self):
        for i in range(3):
            self._save_scored(f"book-{i}", _broken_map(100), "lexical", 100, quality=None)

        first_pass = self.db.backfill_alignment_quality(limit=25)
        second_pass = self.db.backfill_alignment_quality(limit=25)

        self.assertEqual(first_pass, 3)
        self.assertEqual(second_pass, 0)

    def test_backfill_does_not_disturb_last_updated(self):
        """Load-bearing: `backfill_alignment_quality` is a metadata-only pass
        (it only fills `quality_score`/`quality_detail`) and must not stamp
        `last_updated` to "now". `BookAlignment.last_updated` carries
        `onupdate=utcnow`, which SQLAlchemy fires on ANY UPDATE that touches
        the row -- including a plain ORM attribute assignment of
        `quality_score`/`quality_detail` -- unless `last_updated` is itself
        named explicitly in the UPDATE's SET clause. Fails against the
        pre-fix implementation (`row.quality_score = ...` / `row.quality_detail
        = ...` through the ORM), which lets `onupdate` rewrite `last_updated`
        to the moment of the backfill.

        `last_updated` is set to a fixed, clearly-old datetime (2020-01-01)
        before calling the backfill so a "now" stamp is unmistakable, and the
        row is constructed with that value already set (an INSERT, where
        `onupdate` never applies) so the fixture itself cannot be the thing
        that moves it.
        """
        unscored = _broken_map(1000)
        old_stamp = datetime(2020, 1, 1)
        with self.db.get_session() as session:
            row = BookAlignment(
                abs_id="legacy-book",
                alignment_map_json=json.dumps(unscored),
                align_method="lexical",
                total_chars=1000,
                quality_score=None,
                quality_detail=None,
            )
            row.last_updated = old_stamp
            session.add(row)

        with self.db.get_session() as session:
            before = session.query(BookAlignment).filter_by(abs_id="legacy-book").first()
            self.assertEqual(before.last_updated, old_stamp)

        scored_count = self.db.backfill_alignment_quality()

        self.assertEqual(scored_count, 1)
        with self.db.get_session() as session:
            after = session.query(BookAlignment).filter_by(abs_id="legacy-book").first()
            self.assertIsNotNone(after.quality_score)
            self.assertIsNotNone(after.quality_detail)
            self.assertEqual(
                after.last_updated, old_stamp,
                "backfill_alignment_quality must not disturb last_updated "
                "(it is a metadata-only pass, not a re-alignment)",
            )


class TestSaveAlignmentStillAdvancesLastUpdated(_AlignmentQualityTestBase):
    """Guards that the last_updated fix above does not disable legitimate
    provenance updates: unlike the metadata-only backfills, `_save_alignment`
    genuinely replaces the stored map, so it must still advance
    `last_updated`."""

    def test_save_alignment_advances_last_updated_on_rebuild(self):
        old_map = _broken_map(1000)
        old_stamp = datetime(2020, 1, 1)
        with self.db.get_session() as session:
            row = BookAlignment(
                abs_id="rebuilt-book",
                alignment_map_json=json.dumps(old_map),
                align_method="lexical",
                total_chars=1000,
            )
            row.last_updated = old_stamp
            session.add(row)

        service = AlignmentService(self.db, Polisher())
        new_map = _dense_map(1000, 10)
        service._save_alignment("rebuilt-book", new_map, align_method="ctc", total_chars=1000)

        with self.db.get_session() as session:
            after = session.query(BookAlignment).filter_by(abs_id="rebuilt-book").first()
            self.assertEqual(json.loads(after.alignment_map_json), new_map)
            self.assertNotEqual(after.last_updated, old_stamp)
            self.assertGreater(after.last_updated, old_stamp)


if __name__ == "__main__":
    unittest.main()
