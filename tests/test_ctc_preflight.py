"""Issue #426 phase 3: decode-free CTC pre-flight.

Before this phase, `ForcedAligner.align()` decoded the whole audio file
unconditionally and only then consulted `_single_pass_fits` to discover a run
could not proceed -- for a 106,703s/846 MB book that cost ~59s of pure decode
waste before the actual (correct) chunked-or-fallback decode even started.
`ForcedAligner.can_single_pass` and the `AlignmentService.align_forced_and_store`
pre-flight bail let the caller make that decision before paying for a decode.
"""

from unittest.mock import patch

import pytest

from src.db.database_service import DatabaseService
from src.services.alignment_service import AlignmentService
from src.utils.forced_aligner import ForcedAligner
from src.utils.polisher import Polisher


def _fake_load(self) -> None:
    """Populate just enough state for `_target_tokens`/`can_single_pass` to run,
    without importing torch or loading the real MMS_FA bundle."""
    self._dict = {c: i + 1 for i, c in enumerate("abcdefghijklmnopqrstuvwxyz'")}
    self._device = "cpu"
    self._sample_rate = 16000


@pytest.fixture
def service(tmp_path):
    db = DatabaseService(str(tmp_path / "preflight.db"))
    try:
        yield AlignmentService(db, Polisher())
    finally:
        db.db_manager.close()


# --------------------------------------------------------------------------- #
# 1. ForcedAligner.can_single_pass
# --------------------------------------------------------------------------- #

def test_can_single_pass_false_for_a_long_book_with_many_tokens():
    aligner = ForcedAligner()
    text = " ".join(["narration"] * 20000)
    with patch.object(ForcedAligner, "is_available", return_value=True), \
         patch.object(ForcedAligner, "_load", _fake_load), \
         patch.object(ForcedAligner, "_single_pass_fits", return_value=False) as fits, \
         patch.object(ForcedAligner, "_load_audio") as decode:
        assert aligner.can_single_pass(106703.0, text) is False
    decode.assert_not_called()
    fits.assert_called_once()
    # Frames estimated from duration alone, mirroring align()'s
    # `waveform.size(1) // 320` estimate from a decoded waveform.
    _device, est_frames, num_targets = fits.call_args[0]
    assert est_frames == int(106703.0 * 16000) // 320
    assert num_targets > 0


def test_can_single_pass_true_for_a_short_book():
    aligner = ForcedAligner()
    text = "a short alignable sentence"
    with patch.object(ForcedAligner, "is_available", return_value=True), \
         patch.object(ForcedAligner, "_load", _fake_load), \
         patch.object(ForcedAligner, "_single_pass_fits", return_value=True) as fits, \
         patch.object(ForcedAligner, "_load_audio") as decode:
        assert aligner.can_single_pass(5.0, text) is True
    decode.assert_not_called()
    fits.assert_called_once()


def test_can_single_pass_false_when_unavailable():
    aligner = ForcedAligner()
    with patch.object(ForcedAligner, "is_available", return_value=False), \
         patch.object(ForcedAligner, "_load") as load:
        assert aligner.can_single_pass(100.0, "some text") is False
    load.assert_not_called()


def test_can_single_pass_false_when_no_alignable_tokens():
    aligner = ForcedAligner()
    with patch.object(ForcedAligner, "is_available", return_value=True), \
         patch.object(ForcedAligner, "_load", _fake_load), \
         patch.object(ForcedAligner, "_single_pass_fits") as fits:
        assert aligner.can_single_pass(100.0, "1984 —— \U0001f4da") is False
    fits.assert_not_called()


# --------------------------------------------------------------------------- #
# 2. The load-bearing test: the pre-flight bail must skip the decode entirely.
# --------------------------------------------------------------------------- #

def test_align_forced_and_store_skips_decode_when_single_pass_wont_fit(service):
    """With no chunking prior, a long `audio_duration`, and `can_single_pass`
    returning False, `align_forced_and_store` must return False WITHOUT ever
    calling `ForcedAligner.align` (and therefore without decoding). This must
    fail if the pre-flight bail is removed from `align_forced_and_store`.
    """
    text = "x" * 100000
    with patch.object(ForcedAligner, "is_available", return_value=True), \
         patch.object(ForcedAligner, "can_single_pass", return_value=False) as can_single_pass, \
         patch.object(ForcedAligner, "align") as align:
        ok = service.align_forced_and_store(
            "long-book", ["/a.m4b"], text, audio_duration=106703.0,
        )
    assert ok is False
    align.assert_not_called()
    can_single_pass.assert_called_once()
    assert service.database_service.get_alignment_method("long-book") is None


# --------------------------------------------------------------------------- #
# 3. Backward compatibility: audio_duration omitted -> pre-flight skipped.
# --------------------------------------------------------------------------- #

def test_align_forced_and_store_skips_preflight_when_duration_not_given(service):
    fake_map = [{"char": c, "ts": c / 10.0} for c in range(0, 101, 10)]
    with patch.object(ForcedAligner, "is_available", return_value=True), \
         patch.object(ForcedAligner, "can_single_pass") as can_single_pass, \
         patch.object(ForcedAligner, "align", return_value=fake_map) as align:
        ok = service.align_forced_and_store("no-duration", ["/a.m4b"], "x" * 100)
    assert ok is True
    align.assert_called_once()
    can_single_pass.assert_not_called()


def test_align_forced_and_store_skips_preflight_when_duration_is_zero(service):
    fake_map = [{"char": c, "ts": c / 10.0} for c in range(0, 101, 10)]
    with patch.object(ForcedAligner, "is_available", return_value=True), \
         patch.object(ForcedAligner, "can_single_pass") as can_single_pass, \
         patch.object(ForcedAligner, "align", return_value=fake_map) as align:
        ok = service.align_forced_and_store("zero-duration", ["/a.m4b"], "x" * 100, audio_duration=0)
    assert ok is True
    align.assert_called_once()
    can_single_pass.assert_not_called()


# --------------------------------------------------------------------------- #
# 4. A chunking prior (boundaries) bypasses the pre-flight even for a long book.
# --------------------------------------------------------------------------- #

def test_align_forced_and_store_skips_preflight_when_boundaries_exist(service):
    text = "x" * 1000
    prior = [{"char": 0, "ts": 0.0}, {"char": 1000, "ts": 100.0}]
    service._save_alignment("chunked-book", prior, "lexical", total_chars=1000)

    fake_map = [{"char": c, "ts": c / 10.0} for c in range(0, 1001, 50)]
    with patch.object(ForcedAligner, "is_available", return_value=True), \
         patch.object(ForcedAligner, "can_single_pass") as can_single_pass, \
         patch.object(ForcedAligner, "align", return_value=fake_map) as align:
        ok = service.align_forced_and_store(
            "chunked-book", ["/a.m4b"], text, audio_duration=999999.0,
        )
    assert ok is True
    align.assert_called_once()
    can_single_pass.assert_not_called()
    assert align.call_args.kwargs["boundaries"] == prior
