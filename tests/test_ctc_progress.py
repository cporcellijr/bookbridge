"""Progress reporting from the CTC model pass.

The model pass (emissions) is nearly all of a CTC run's time: about 8 of 9 minutes
for a 2.5-hour book on a 2-CPU container. Before this, the Job Queue sat at the
start of the transcription stage (10%) for that whole time.

The aligners report seconds of audio covered; `AlignmentService` turns that into a
0.0-1.0 fraction of the book's known duration, at most once per percent.
"""
from unittest.mock import patch

import numpy as np
import pytest

from src.db.database_service import DatabaseService
from src.services.alignment_service import AlignmentService
from src.utils import quartznet_aligner as qn
from src.utils.forced_aligner import ForcedAligner, report_audio_progress
from src.utils.polisher import Polisher
from src.utils.quartznet_aligner import QuartzNetAligner


class _FakeSession:
    def run(self, _outputs, feeds):
        signal = feeds["signal"]
        frames = (signal.shape[-1] - 2 * qn._PAD_SAMPLES) // 320 + 1
        return [np.zeros((1, frames, 29), dtype=np.float32)]


def _quartznet():
    aligner = QuartzNetAligner()
    aligner._session = _FakeSession()
    aligner._dict = {c: i + 1 for i, c in enumerate(qn._VOCAB)}
    aligner._device = "cpu"
    return aligner


def _windows(*sizes):
    return [np.zeros(size, dtype=np.float32) for size in sizes]


# --------------------------------------------------------------------------- #
# Aligners report seconds covered
# --------------------------------------------------------------------------- #

def test_quartznet_reports_seconds_covered_after_each_window():
    tail = 10 * qn._SAMPLE_RATE
    windows = _windows(qn._WINDOW_SAMPLES, qn._WINDOW_SAMPLES, tail)
    seen = []
    with patch.object(QuartzNetAligner, "_pcm_windows", return_value=iter(windows)):
        _quartznet()._stream_emissions(["/a.m4b"], progress_callback=seen.append)

    window_seconds = qn._WINDOW_SAMPLES / qn._SAMPLE_RATE
    assert seen == pytest.approx([0.0, window_seconds, 2 * window_seconds, 2 * window_seconds + 10])


def test_quartznet_emissions_for_and_align_pass_the_callback_to_the_stream():
    aligner = _quartznet()
    seen = []
    with patch.object(QuartzNetAligner, "is_available", return_value=True), \
         patch.object(QuartzNetAligner, "_load"), \
         patch.object(QuartzNetAligner, "_pcm_windows", return_value=iter(_windows(qn._WINDOW_SAMPLES))):
        assert aligner.emissions_for(["/a.m4b"], progress_callback=seen.append) is not None
    assert seen[-1] == pytest.approx(qn._WINDOW_SAMPLES / qn._SAMPLE_RATE)

    seen.clear()
    with patch.object(QuartzNetAligner, "_pcm_windows", return_value=iter(_windows(qn._WINDOW_SAMPLES))):
        aligner._decode_and_emit(["/a.m4b"], None, 10, progress_callback=seen.append)
    assert seen[-1] == pytest.approx(qn._WINDOW_SAMPLES / qn._SAMPLE_RATE)


def test_quartznet_without_a_callback_is_unchanged():
    with patch.object(QuartzNetAligner, "_pcm_windows", return_value=iter(_windows(qn._WINDOW_SAMPLES))):
        emission, _spf = _quartznet()._stream_emissions(["/a.m4b"])
    assert emission.shape == (1, qn._FRAMES_PER_WINDOW, 29)


def test_a_failing_callback_never_fails_the_pass():
    def boom(_seconds):
        raise RuntimeError("database is locked")

    with patch.object(QuartzNetAligner, "_pcm_windows", return_value=iter(_windows(qn._WINDOW_SAMPLES))):
        emission, _spf = _quartznet()._stream_emissions(["/a.m4b"], progress_callback=boom)
    assert emission.shape[1] == qn._FRAMES_PER_WINDOW
    report_audio_progress(None, 1.0)  # no callback is a no-op


def test_mms_reports_seconds_covered_after_each_window():
    torch = pytest.importorskip("torch")

    class _Model:
        def __call__(self, batch):
            return torch.zeros(batch.size(0), batch.size(1) // 320, 29), None

    aligner = ForcedAligner()
    aligner._model = _Model()
    aligner._device = "cpu"
    aligner._sample_rate = 16000
    seconds = 95
    seen = []
    aligner._emissions(torch.zeros(1, seconds * 16000), progress_callback=seen.append)

    assert seen == sorted(seen)
    assert seen[-1] == pytest.approx(seconds)


# --------------------------------------------------------------------------- #
# AlignmentService turns seconds into a throttled fraction
# --------------------------------------------------------------------------- #

def test_audio_progress_is_a_clamped_fraction_in_one_percent_steps():
    seen = []
    on_seconds = AlignmentService._audio_progress(seen.append, 1000.0)
    for seconds in [0, 3, 9, 10, 15, 25, 500, 999, 1000, 1000, 1200]:
        on_seconds(seconds)
    assert seen == [0.0, 0.01, 0.025, 0.5, 0.999, 1.0]


@pytest.mark.parametrize("callback, duration", [(None, 100.0), (print, None), (print, 0), (print, -5)])
def test_audio_progress_needs_a_callback_and_a_duration(callback, duration):
    assert AlignmentService._audio_progress(callback, duration) is None


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setenv("CTC_MODEL", "mms_fa")
    db = DatabaseService(str(tmp_path / "progress.db"))
    try:
        yield AlignmentService(db, Polisher())
    finally:
        db.db_manager.close()


def _fake_map():
    return [{"char": c, "ts": c / 10.0} for c in range(0, 101, 10)]


def test_align_forced_and_store_reports_the_align_pass_as_a_fraction(service):
    def fake_align(_paths, _text, **kwargs):
        for seconds in (0, 50, 100):
            kwargs["progress_callback"](seconds)
        return _fake_map()

    seen = []
    with patch.object(ForcedAligner, "is_available", return_value=True), \
         patch.object(ForcedAligner, "can_single_pass", return_value=True), \
         patch.object(ForcedAligner, "align", side_effect=fake_align):
        ok = service.align_forced_and_store("book-1", ["/a.m4b"], "x" * 100,
                                            audio_duration=100.0, progress_callback=seen.append)
    assert ok is True
    assert seen == [0.0, 0.5, 1.0]


def test_align_forced_and_store_reports_the_chapter_search_pass(service):
    """With no prior map, the model runs in `emissions_for` and `align` reuses its
    output, so progress comes from `emissions_for` and `align` gets no callback."""
    seen = []

    def fake_emissions_for(_paths, progress_callback=None):
        progress_callback(30.0)
        progress_callback(60.0)
        return np.zeros((1, 10, 29)), 0.02

    with patch.object(ForcedAligner, "is_available", return_value=True), \
         patch.object(ForcedAligner, "can_single_pass", return_value=False), \
         patch.object(ForcedAligner, "emissions_for", side_effect=fake_emissions_for), \
         patch.object(AlignmentService, "_search_prior", return_value=None):
        ok = service.align_forced_and_store(
            "book-2", ["/a.m4b"], "x" * 100, spine_chapters=[{"start": 0, "end": 100}],
            audio_duration=60.0, progress_callback=seen.append)
    assert ok is False  # the search prior was refused; progress still reported
    assert seen == [0.5, 1.0]


def test_align_forced_and_store_without_a_callback_passes_none_to_align(service):
    with patch.object(ForcedAligner, "is_available", return_value=True), \
         patch.object(ForcedAligner, "can_single_pass", return_value=True), \
         patch.object(ForcedAligner, "align", return_value=_fake_map()) as align:
        assert service.align_forced_and_store("book-3", ["/a.m4b"], "x" * 100, audio_duration=100.0)
    assert "progress_callback" not in align.call_args.kwargs
