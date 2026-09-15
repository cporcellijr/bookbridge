"""Issue #426 phase 2: every alignment write funnels through `_publish_map`.

Phase 1 (`src/services/map_quality.py`) added a pure score for an alignment map's
positional reliability. Before this seam, only the CTC write site backed up and
checked anything before overwriting the stored map, so re-aligning a book that
already held a good CTC map was unconditionally destroyed by a fresh lexical
rebuild before anything decided the lexical map was better (Four Past Midnight:
the CTC map scored 0.324, the lexical rebuild that clobbered it scored 0.200).
"""

import json
from typing import Dict, List

import pytest

from src.db.database_service import DatabaseService
from src.db.models import BookAlignment
from src.services import map_quality
from src.services.alignment_service import AlignmentService
from src.services.segment_fit import Segment
from src.utils.polisher import Polisher


@pytest.fixture
def service(tmp_path):
    db = DatabaseService(str(tmp_path / "publish_seam.db"))
    try:
        yield AlignmentService(db, Polisher())
    finally:
        db.db_manager.close()


def _dense_map(length: int, step: int, ts_per_char: float = 0.01) -> List[Dict]:
    """An evenly-paced, densely-anchored synthetic map: scores near 1.0."""
    return [{"char": c, "ts": round(c * ts_per_char, 3)} for c in range(0, length + 1, step)]


class _EmptyStorytellerTranscript:
    """A Storyteller transcript with no chapters, forcing the linear-fallback path."""
    chapters: List[Dict] = []

    def get_global_duration(self) -> float:
        return 500.0


# --------------------------------------------------------------------------- #
# The load-bearing regression test
# --------------------------------------------------------------------------- #

def test_align_and_store_does_not_clobber_a_good_ctc_map_with_a_worse_lexical_rebuild(service):
    """A re-align that would produce a materially worse lexical map must not
    destroy a good CTC map already on file (the Four Past Midnight defect,
    issue #426 phase 2). This must fail if `_publish_map` is reduced to an
    unconditional save.
    """
    text = "x" * 1000
    good_ctc = _dense_map(1000, 10)
    service._save_alignment("book", good_ctc, "ctc", total_chars=1000)

    # Clears no gate on its own merits -- it is just sparse and unevenly scored
    # relative to the CTC map already on file.
    worse_lexical = [{"char": c, "ts": c / 100.0} for c in range(0, 1001, 200)]
    service._generate_alignment_map_with_method = (
        lambda segments, full_text, abs_id=None, spine_chapters=None: (worse_lexical, "lexical", None))

    result = service.align_and_store("book", [{"start": 0.0, "end": 1.0, "text": "x"}], text)

    # A vetoed write is not a failure: the book still has a valid, better map.
    assert result is True
    assert service.database_service.get_alignment_method("book") == "ctc"
    assert service._get_alignment("book") == good_ctc


# --------------------------------------------------------------------------- #
# The veto must not block the normal CTC upgrade path
# --------------------------------------------------------------------------- #

def test_healthy_ctc_replaces_healthy_lexical_of_similar_quality(service):
    """Two maps of very different real precision but equally healthy gross
    structure score near-identically -- the veto must not treat that tie as a
    regression and block the normal CTC upgrade.
    """
    lexical = _dense_map(1000, 10)
    service._save_alignment("book", lexical, "lexical", total_chars=1000)

    ctc = _dense_map(1000, 10)
    assert service._publish_map("book", ctc, "ctc", total_chars=1000) is True
    assert service.database_service.get_alignment_method("book") == "ctc"
    assert service._get_alignment("book") == ctc


# --------------------------------------------------------------------------- #
# A veto takes no backup
# --------------------------------------------------------------------------- #

def test_veto_takes_no_backup(service):
    """After a vetoed write, `restore_previous_alignment` must still return
    whatever it would have before -- the backup slot must not be overwritten
    with the vetoed challenger's predecessor.
    """
    original = _dense_map(1000, 10)
    service._save_alignment("book", original, "lexical", total_chars=1000)

    good_ctc = _dense_map(1000, 5)
    assert service._publish_map("book", good_ctc, "ctc", total_chars=1000) is True
    # The backup slot now holds `original` (the pre-CTC lexical map).

    worse_challenger = [{"char": c, "ts": c / 100.0} for c in range(0, 1001, 200)]
    assert service._publish_map("book", worse_challenger, "ctc", total_chars=1000) is False
    # The veto must not have touched the current map or the backup slot.
    assert service.database_service.get_alignment_method("book") == "ctc"
    assert service._get_alignment("book") == good_ctc

    assert service.restore_previous_alignment("book") is True
    assert service.database_service.get_alignment_method("book") == "lexical"
    assert service._get_alignment("book") == original


# --------------------------------------------------------------------------- #
# Unconditional-replace cases
# --------------------------------------------------------------------------- #

def test_total_chars_mismatch_always_replaces_even_when_incumbent_scores_higher(service):
    good_incumbent = _dense_map(1000, 5)
    service._save_alignment("book", good_incumbent, "ctc", total_chars=1000)

    # Scores far worse than the incumbent, but was built against a different
    # ebook length -- the two scores are not comparable, so the new map wins.
    sparse_challenger = [{"char": c, "ts": c / 50.0} for c in range(0, 2001, 500)]
    assert service._publish_map("book", sparse_challenger, "lexical", total_chars=2000) is True
    assert service.database_service.get_alignment_method("book") == "lexical"
    assert service._get_alignment("book") == sparse_challenger


def test_null_incumbent_total_chars_always_replaces_even_when_incumbent_scores_higher(service):
    """A legacy incumbent with no recorded `total_chars` (84% of stored maps, per a
    live DB measurement) cannot prove which ebook it was built against, so it is
    not a trustworthy regression baseline -- a challenger that knows its own
    ebook length must replace it even when the incumbent scores higher.
    """
    good_incumbent = _dense_map(1000, 5)
    service._save_alignment("book", good_incumbent, "lexical", total_chars=None)
    assert service._get_alignment_total_chars("book") is None

    sparse_challenger = [{"char": c, "ts": c / 50.0} for c in range(0, 1001, 500)]
    assert service._publish_map("book", sparse_challenger, "ctc", total_chars=1000) is True
    assert service.database_service.get_alignment_method("book") == "ctc"
    assert service._get_alignment("book") == sparse_challenger


def test_linear_incumbent_always_replaced_regardless_of_score(service):
    service._save_alignment("book", [{"char": 0, "ts": 0.0}, {"char": 1000, "ts": 50.0}],
                            "linear", total_chars=1000)

    sparse_worse = [{"char": c, "ts": c / 50.0} for c in range(0, 1001, 500)]
    assert service._publish_map("book", sparse_worse, "ctc", total_chars=1000) is True
    assert service.database_service.get_alignment_method("book") == "ctc"
    assert service._get_alignment("book") == sparse_worse


# --------------------------------------------------------------------------- #
# Storyteller's linear fallback must not destroy a good incumbent
# --------------------------------------------------------------------------- #

def test_storyteller_linear_fallback_does_not_destroy_a_good_incumbent(service):
    text = "x" * 1000
    good_ctc = _dense_map(1000, 5)
    service._save_alignment("book", good_ctc, "ctc", total_chars=1000)

    result = service.align_storyteller_and_store("book", _EmptyStorytellerTranscript(), ebook_text=text)

    assert result is True
    assert service.database_service.get_alignment_method("book") == "ctc"
    assert service._get_alignment("book") == good_ctc


# --------------------------------------------------------------------------- #
# Issue #426 phase 4: a map's quality is persisted, not thrown away
# --------------------------------------------------------------------------- #

def _row(service, abs_id: str) -> BookAlignment:
    with service.database_service.get_session() as session:
        row = session.query(BookAlignment).filter_by(abs_id=abs_id).first()
        session.expunge(row)
        return row


def test_save_alignment_with_quality_persists_score_and_round_tripping_detail(service):
    amap = _dense_map(1000, 10)
    quality = map_quality.score_map(amap)

    service._save_alignment("book", amap, "lexical", total_chars=1000, quality=quality)

    row = _row(service, "book")
    assert row.quality_score == quality.score
    detail = json.loads(row.quality_detail)
    assert detail["anchors"] == quality.anchors
    assert detail["span_chars"] == quality.span_chars


def test_save_alignment_without_quality_leaves_existing_score_untouched(service):
    """Same discipline as `total_chars`: a caller that didn't score the map (a
    bare re-save) must not wipe a previously recorded score."""
    amap = _dense_map(1000, 10)
    quality = map_quality.score_map(amap)
    service._save_alignment("book", amap, "lexical", total_chars=1000, quality=quality)

    service._save_alignment("book", amap, "lexical", total_chars=1000)

    assert _row(service, "book").quality_score == quality.score


def test_save_alignment_degenerate_density_spread_infinity_does_not_raise_and_stores_null(service):
    """The infinity test (issue #426 phase 4): `density_spread` is legitimately
    `float('inf')` for a degenerate map, and `json.dumps(float('inf'))` emits the
    bare token ``Infinity``, which is not valid JSON. This must fail if the
    `math.isfinite` guard in `map_quality.quality_detail_json` is removed.
    """
    degenerate_map = [{"char": 0, "ts": 0.0}, {"char": 100000, "ts": 1000.0}]
    quality = map_quality.score_map(degenerate_map)
    assert quality.density_spread == float("inf")

    service._save_alignment("book", degenerate_map, "lexical", total_chars=100000, quality=quality)

    row = _row(service, "book")
    assert row.quality_score == quality.score
    # CPython's own json.loads happily accepts the bare `Infinity` token as an
    # extension, so the regression this guards against is invisible to a plain
    # round-trip check -- assert the raw text never contains it (a stricter, or
    # non-Python, JSON parser would reject it outright) and that it decoded to
    # `null`, not a huge finite stand-in.
    detail_text = row.quality_detail
    assert "Infinity" not in detail_text
    detail = json.loads(detail_text)
    assert detail["density_spread"] is None


def test_publish_map_persists_score_on_store_path(service):
    good = _dense_map(1000, 10)
    assert service._publish_map("book", good, "lexical", total_chars=1000) is True

    row = _row(service, "book")
    assert row.quality_score == map_quality.score_map(good).score
    assert row.quality_detail is not None


def test_publish_map_does_not_write_a_score_on_the_veto_path(service):
    good_ctc = _dense_map(1000, 10)
    good_quality = map_quality.score_map(good_ctc)
    service._save_alignment("book", good_ctc, "ctc", total_chars=1000, quality=good_quality)

    worse_lexical = [{"char": c, "ts": c / 100.0} for c in range(0, 1001, 200)]
    assert service._publish_map("book", worse_lexical, "lexical", total_chars=1000) is False

    # A vetoed write must leave the incumbent's score exactly as it was --
    # never overwritten with the vetoed challenger's score.
    row = _row(service, "book")
    assert row.quality_score == good_quality.score
    assert row.align_method == "ctc"


# --------------------------------------------------------------------------- #
# Issue #426 phase 3: incumbent and challenger are each scored with their OWN
# segments, never each other's.
#
# The reordered fixture below (four independently-placed, perfectly-paced
# segments -- see tests/test_map_quality.py::TestSegmentAwareDensitySpread for
# the same construction and its measured numbers) scores ~0.938 with its own
# segments but only ~0.488 scored blind (no segments at all). A mediocre plain
# map, deliberately tuned to score ~0.622, sits strictly between those two
# numbers -- so which way the veto falls is a direct, decisive tell for
# whether `_publish_map` fetched and used the right side's segment index.
# --------------------------------------------------------------------------- #

def _reordered_segmented_fixture():
    """Four chapter-sized, disjoint char ranges, each perfectly evenly paced
    on its own but independently placed in `ts` and not aligned to the
    whole-map 20-slice partition. Returns `(flat_map, segments)` where
    `segments` are `Segment` dataclass instances (the shape a challenger
    passes to `_publish_map`; `_save_alignment` converts them to dicts for
    storage, which is also the shape `_get_segments` later returns for an
    incumbent)."""
    segment_defs = [
        (0, 50000, 12.0, 187, 44415.0),
        (50000, 50000, 14.0, 211, 500.0),
        (100000, 50000, 16.0, 197, 90000.0),
        (150000, 50000, 13.0, 203, 9000.0),
    ]
    flat_map: List[Dict] = []
    segments: List[Segment] = []
    for char_start, span, rate, n, ts0 in segment_defs:
        points = [{"char": char_start + int(span * i / n),
                   "ts": ts0 + int(span * i / n) / rate}
                  for i in range(n)]
        flat_map.extend(points)
        segments.append(Segment(char_start=char_start, char_end=char_start + span,
                                ts_start=points[0]["ts"], ts_end=points[-1]["ts"],
                                inliers=len(points), residual=0.0))
    flat_map.sort(key=lambda p: p["char"])
    return flat_map, segments


def _mediocre_plain_map(total_chars: int = 200000) -> List[Dict]:
    """Scores ~0.622 -- strictly between the reordered fixture's blind
    (~0.488) and segment-aware (~0.938) scores."""
    base = [{"char": c, "ts": c / 50.0} for c in range(0, total_chars + 1, 4000)]
    lo, hi = int(total_chars * 0.4), int(total_chars * 0.4) + int(total_chars * 0.2)
    return [p for p in base if not (lo < p["char"] < hi)]


def test_publish_map_scores_incumbent_with_its_own_stored_segments(service):
    """A challenger that would only be a regression against the incumbent's
    real, segment-aware score (~0.938) -- not against how the incumbent
    would score blind (~0.488) -- must still be vetoed. This fails if
    `_publish_map` stops fetching `self._get_segments(abs_id)` for the
    incumbent (or drops it on the floor instead of passing it to
    `score_map`): the incumbent would then score ~0.488, the mediocre
    challenger (~0.622) would no longer look like a regression, and the
    genuinely-better incumbent would be destroyed.
    """
    reordered_map, segments = _reordered_segmented_fixture()
    service._save_alignment("book", reordered_map, "ctc", total_chars=200000,
                            segments=segments)

    mediocre_challenger = _mediocre_plain_map()
    result = service._publish_map("book", mediocre_challenger, "lexical", total_chars=200000)

    assert result is False
    assert service.database_service.get_alignment_method("book") == "ctc"
    assert service._get_alignment("book") == reordered_map


def test_publish_map_scores_challenger_with_its_own_segments_not_incumbents(service):
    """The mirror case: a challenger that is a genuine improvement only once
    scored with its OWN segments (~0.938, vs ~0.488 scored blind) must be
    accepted against a mediocre plain incumbent (~0.622) that has no
    segments of its own. This fails if `_publish_map` stops passing its
    `segments` argument through to the challenger's `score_map` call: the
    challenger would then score ~0.488, look like a regression against the
    ~0.622 incumbent, and be wrongly vetoed.
    """
    mediocre_incumbent = _mediocre_plain_map()
    service._save_alignment("book", mediocre_incumbent, "lexical", total_chars=200000)
    assert service._get_segments("book") is None

    reordered_challenger, segments = _reordered_segmented_fixture()
    result = service._publish_map("book", reordered_challenger, "ctc", total_chars=200000,
                                  segments=segments)

    assert result is True
    assert service.database_service.get_alignment_method("book") == "ctc"
    assert service._get_alignment("book") == reordered_challenger
    stored_segments = service._get_segments("book")
    assert stored_segments == [
        {"char_start": s.char_start, "char_end": s.char_end,
         "ts_start": s.ts_start, "ts_end": s.ts_end}
        for s in segments
    ]
