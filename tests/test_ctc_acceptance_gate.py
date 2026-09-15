"""Issue #426: CTC acceptance gate + alignment rollback.

The gate refuses to store a degenerate/near-linear CTC map, and refuses to replace
an existing map with one that leaves a materially larger interpolated gap — so a
remap can only keep or improve a book's alignment. Before an accepted CTC map
overwrites the previous one, the previous map is copied to a backup table so a bad
remap is instantly reversible (``restore_previous_alignment``).
"""

from unittest.mock import patch

import pytest

from src.db.database_service import DatabaseService
from src.services.alignment_service import AlignmentService
from src.utils.polisher import Polisher


@pytest.fixture
def service(tmp_path):
    db = DatabaseService(str(tmp_path / "gate.db"))
    try:
        yield AlignmentService(db, Polisher())
    finally:
        db.db_manager.close()


def _store_ctc(service, abs_id, returned_map, text):
    with patch("src.utils.forced_aligner.ForcedAligner.is_available", return_value=True), \
         patch("src.utils.forced_aligner.ForcedAligner.align", return_value=returned_map):
        return service.align_forced_and_store(abs_id, ["/a.m4b"], text)


def _dense_map(length, step, ts_per_char=0.01):
    return [{"char": c, "ts": round(c * ts_per_char, 3)} for c in range(0, length + 1, step)]


# --------------------------------------------------------------------------- #
# _max_gap_fraction (pure)
# --------------------------------------------------------------------------- #

def test_max_gap_fraction_degenerate_and_empty():
    assert AlignmentService._max_gap_fraction([]) == 1.0
    assert AlignmentService._max_gap_fraction([{"char": 5, "ts": 1.0}]) == 1.0
    # Two points => the single span is the whole range.
    assert AlignmentService._max_gap_fraction([{"char": 0, "ts": 0.0},
                                               {"char": 100, "ts": 1.0}]) == 1.0


def test_max_gap_fraction_uses_largest_gap_and_global_char():
    amap = [{"char": 0, "ts": 0.0}, {"char": 50, "ts": 1.0}, {"char": 100, "ts": 2.0}]
    assert AlignmentService._max_gap_fraction(amap) == pytest.approx(0.5)
    # Legacy points carry 'global_char' instead of 'char'.
    legacy = [{"global_char": 0, "ts": 0.0}, {"global_char": 100, "ts": 1.0},
              {"global_char": 110, "ts": 2.0}]
    assert AlignmentService._max_gap_fraction(legacy) == pytest.approx(100 / 110)


# --------------------------------------------------------------------------- #
# Gate: absolute degenerate rejection
# --------------------------------------------------------------------------- #

def test_degenerate_map_rejected_when_no_prior(service):
    text = "x" * 1000
    ok = _store_ctc(service, "book", [{"char": 0, "ts": 0.0}, {"char": 1000, "ts": 50.0}], text)
    assert ok is False
    # Nothing stored, nothing to roll back.
    assert service.database_service.get_alignment_method("book") is None
    assert service.restore_previous_alignment("book") is False


def test_degenerate_map_does_not_overwrite_existing_map(service):
    text = "x" * 1000
    prior = _dense_map(1000, 10)
    service._save_alignment("book", prior, "lexical", total_chars=1000)

    ok = _store_ctc(service, "book", [{"char": 0, "ts": 0.0}, {"char": 1000, "ts": 50.0}], text)
    assert ok is False
    # The good lexical map is untouched and no backup was taken (gate ran first).
    assert service.database_service.get_alignment_method("book") == "lexical"
    assert service._get_alignment("book") == prior
    assert service.restore_previous_alignment("book") is False


# --------------------------------------------------------------------------- #
# Gate: no-regression vs the existing map
# --------------------------------------------------------------------------- #

def test_map_worse_than_prior_is_rejected(service):
    text = "x" * 1000
    prior = _dense_map(1000, 10)                       # worst gap 1% of the text
    service._save_alignment("book", prior, "lexical", total_chars=1000)

    # Clears the absolute gate (worst gap 20% < 25%) but is far sparser than the prior.
    worse = [{"char": c, "ts": c / 100.0} for c in range(0, 1001, 200)]
    ok = _store_ctc(service, "book", worse, text)
    assert ok is False
    assert service.database_service.get_alignment_method("book") == "lexical"
    assert service._get_alignment("book") == prior


def test_ctc_replaces_a_linear_prior(service):
    text = "x" * 1000
    service._save_alignment("book", [{"char": 0, "ts": 0.0}, {"char": 1000, "ts": 50.0}],
                            "linear", total_chars=1000)
    good = _dense_map(1000, 10)
    ok = _store_ctc(service, "book", good, text)
    assert ok is True
    assert service.database_service.get_alignment_method("book") == "ctc"


# --------------------------------------------------------------------------- #
# Backup + restore round trip
# --------------------------------------------------------------------------- #

def test_accepted_ctc_backs_up_prior_and_is_restorable(service):
    text = "x" * 1000
    prior = _dense_map(1000, 10)
    service._save_alignment("book", prior, "lexical", total_chars=1000)

    good = _dense_map(1000, 5)
    assert _store_ctc(service, "book", good, text) is True
    assert service.database_service.get_alignment_method("book") == "ctc"
    assert service._get_alignment("book") == good

    # Roll back to exactly the pre-CTC lexical map.
    assert service.restore_previous_alignment("book") is True
    assert service.database_service.get_alignment_method("book") == "lexical"
    assert service._get_alignment("book") == prior
    assert service.database_service.get_alignment_total_chars("book") == 1000

    # The backup is kept, so a restore can be repeated.
    assert service.restore_previous_alignment("book") is True
    assert service._get_alignment("book") == prior


def test_restore_without_backup_returns_false(service):
    assert service.restore_previous_alignment("never-aligned") is False


# --------------------------------------------------------------------------- #
# Chunking source: a CTC map must never window its own successor
# --------------------------------------------------------------------------- #

def test_ctc_never_windows_against_its_own_prior_map(service):
    """A CTC pass must not take chunk boundaries from a previous CTC map (#426).

    ``_chunked_word_times`` derives each chunk's audio window from ``boundaries``.
    Handing it the map this run is about to replace makes any error in that map
    re-derive the same windows and reproduce itself, so no remap can ever escape it
    — Four Past Midnight rode that loop with a bit-identical gap start char across a
    full re-alignment. A transcript-derived map is independent evidence and is still
    trusted.
    """
    text = "word " * 4000
    good = _dense_map(len(text), 50)
    seen = []

    def capture(_audio, _text, text_range=None, boundaries=None, exclude_spans=None):
        seen.append(boundaries)
        return good

    def remap():
        with patch("src.utils.forced_aligner.ForcedAligner.is_available", return_value=True), \
             patch("src.utils.forced_aligner.ForcedAligner.align", side_effect=capture):
            return service.align_forced_and_store("book", ["/a.m4b"], text)

    # A lexical prior is independent of CTC, so it still bounds the chunk windows.
    service._save_alignment("book", good, "lexical", total_chars=len(text))
    assert remap() is True
    assert seen[-1] == good, "a lexical prior should still bound the chunk windows"

    # That store made the map 'ctc'. The next remap must not window against it.
    assert service.database_service.get_alignment_method("book") == "ctc"
    assert remap() is True
    assert seen[-1] is None, "a CTC prior must not bound the chunk windows"
