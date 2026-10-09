"""QuartzNet15x5 CTC aligner on onnxruntime (src/utils/quartznet_aligner.py).

The ONNX session is faked: each test pins one contract of the real model's
wrapping, which was verified live against Storyteller's own cached QuartzNet
emissions (99.97% argmax agreement, identical frame counts).
"""
import hashlib
import shutil
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from src.services.alignment_service import AlignmentService
from src.utils import quartznet_aligner as qn
from src.utils.forced_aligner import ForcedAligner
from src.utils.quartznet_aligner import QuartzNetAligner, ctc_viterbi_first_frames

BLANK = 28


def _brute_force_first_frames(log_probs, labels, blank):
    """Exhaustive best CTC path for tiny inputs (reference for the Viterbi)."""
    import itertools

    frames = log_probs.shape[0]
    best_score, best = -np.inf, None
    classes = sorted(set(labels) | {blank})
    for path in itertools.product(classes, repeat=frames):
        collapsed, prev = [], None
        for c in path:
            if c != blank and c != prev:
                collapsed.append(c)
            prev = c
        if collapsed != list(labels):
            continue
        score = sum(log_probs[t, c] for t, c in enumerate(path))
        if score > best_score:
            starts, prev = [], None
            for t, c in enumerate(path):
                if c != blank and c != prev:
                    starts.append(t)
                prev = c
            best_score, best = score, starts
    return best


def test_viterbi_matches_brute_force_on_small_inputs():
    rng = np.random.default_rng(3)
    for labels in ([1, 2], [1, 1], [2, 1, 2]):
        log_probs = np.log(rng.dirichlet(np.ones(3), size=6)).astype(np.float32)
        remapped = {1: 0, 2: 1}
        assert ctc_viterbi_first_frames(log_probs, [remapped[x] for x in labels], blank=2) == \
            _brute_force_first_frames(log_probs, [remapped[x] for x in labels], 2)


def test_viterbi_matches_torchaudio_forced_align():
    """Same token start frames as torchaudio's forced_align on random emissions."""
    torch = pytest.importorskip("torch")
    F = pytest.importorskip("torchaudio.functional")
    rng = np.random.default_rng(11)
    for _ in range(20):
        frames, classes = int(rng.integers(40, 120)), 6
        labels = [int(x) for x in rng.integers(1, classes, size=int(rng.integers(3, 12)))]
        log_probs = np.log(rng.dirichlet(np.ones(classes), size=frames)).astype(np.float32)
        aligned, scores = F.forced_align(torch.from_numpy(log_probs)[None],
                                         torch.tensor([labels], dtype=torch.int32), blank=0)
        spans = F.merge_tokens(aligned[0], scores[0], blank=0)
        assert ctc_viterbi_first_frames(log_probs, labels, blank=0) == [s.start for s in spans]


def test_a_wildcard_absorbs_speech_outside_the_target():
    """Ten frames of other speech precede "a b". Plain Viterbi must spend them on the
    target and starts "a" at frame 0; skippable wildcards at both ends (ghost-story's
    star, -3 per frame) take them instead, so "a" starts where it is spoken."""
    log_probs = np.full((20, 3), -10.0, dtype=np.float32)
    log_probs[:10, 1] = -9.0          # other speech: "a" is merely the least bad label
    log_probs[10:, 0] = 0.0           # blank after it...
    log_probs[10, 1] = log_probs[14, 2] = 0.0
    log_probs[10, 0] = log_probs[14, 0] = -10.0

    assert ctc_viterbi_first_frames(log_probs, [1, 2], blank=0)[0] == 0
    assert ctc_viterbi_first_frames(log_probs, [3, 1, 2, 3], blank=0, star=3)[1:3] == [10, 14]
    # A wildcard with nothing to absorb is skipped rather than forced onto a frame.
    assert ctc_viterbi_first_frames(log_probs[10:], [3, 1, 2, 3], blank=0, star=3)[1:3] == [0, 4]


def test_viterbi_without_a_path_returns_none():
    log_probs = np.zeros((2, 3), dtype=np.float32)
    assert ctc_viterbi_first_frames(log_probs, [1, 1], blank=0) is None


class _FakeSession:
    """Records each ``signal`` and answers with log-probs of the right frame count."""

    def __init__(self):
        self.signals = []

    def run(self, _outputs, feeds):
        signal = feeds["signal"]
        self.signals.append(signal)
        frames = (signal.shape[-1] - 2 * qn._PAD_SAMPLES) // 160 // 2 + 1
        log_probs = np.full((1, frames, 29), -20.0, dtype=np.float32)
        log_probs[0, :, 0] = np.log(0.25)        # space
        log_probs[0, :, BLANK] = np.log(0.5)
        return [log_probs]


def _aligner_with_fake_session():
    aligner = QuartzNetAligner()
    aligner._session = _FakeSession()
    aligner._dict = {c: i + 1 for i, c in enumerate(qn._VOCAB)}
    aligner._device = "cpu"
    return aligner


def test_emissions_preemphasise_pad_fold_space_and_trim_the_window_edge():
    aligner = _aligner_with_fake_session()
    rng = np.random.default_rng(5)
    samples = rng.standard_normal(qn._WINDOW_SAMPLES + 16000).astype(np.float32)

    emission = aligner._emissions(samples[None, :])

    first, tail = aligner._session.signals
    assert first.shape == (1, 1, qn._WINDOW_SAMPLES + 2 * qn._PAD_SAMPLES)
    body = first[0, 0, qn._PAD_SAMPLES:-qn._PAD_SAMPLES]
    assert body[0] == pytest.approx(samples[0])
    np.testing.assert_allclose(body[1:], samples[1:qn._WINDOW_SAMPLES] - 0.97 * samples[:qn._WINDOW_SAMPLES - 1],
                               rtol=1e-6, atol=1e-6)
    np.testing.assert_array_equal(first[0, 0, :qn._PAD_SAMPLES], body[qn._PAD_SAMPLES:0:-1])
    # The full window's extra edge frame is dropped: 38 s = exactly 1900 frames.
    tail_frames = (tail.shape[-1] - 2 * qn._PAD_SAMPLES) // 320 + 1
    assert emission.shape == (1, qn._FRAMES_PER_WINDOW + tail_frames, 29)
    np.testing.assert_allclose(emission[0, :, BLANK], np.log(0.75), rtol=1e-6)
    assert (emission[0, :, 0] == qn._FOLDED_SPACE_SCORE).all()


def test_a_sliver_of_trailing_audio_is_not_run():
    aligner = _aligner_with_fake_session()
    aligner._emissions(np.zeros((1, qn._WINDOW_SAMPLES + 100), dtype=np.float32))
    assert len(aligner._session.signals) == 1


def test_word_starts_apply_the_onset_offset_and_frame_offset():
    aligner = _aligner_with_fake_session()
    log_probs = np.full((1, 40, 29), -10.0, dtype=np.float32)
    log_probs[0, :, BLANK] = 0.0
    log_probs[0, 10, 1] = 0.0    # "a" at frame 10
    log_probs[0, 30, 2] = 0.0    # "b" at frame 30
    log_probs[0, 10, BLANK] = log_probs[0, 30, BLANK] = -10.0

    times = aligner._segment_word_times(None, None, log_probs, [[1], [2]], 0.02, frame_offset=100)

    assert times == [pytest.approx(110 * 0.02 - 0.28), pytest.approx(130 * 0.02 - 0.28)]
    early = aligner._segment_word_times(None, None, log_probs[:, :12], [[1]], 0.02)
    assert early == [pytest.approx(0.0)]  # 0.20 s - 0.28 s is clamped, never negative


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("CTC_QUARTZNET_MODEL_PATH", raising=False)
    return tmp_path


def test_a_configured_model_file_or_folder_is_used_without_downloading(data_dir, monkeypatch):
    folder = data_dir / "storyteller-copy"
    folder.mkdir()
    (folder / "model.onnx").write_bytes(b"onnx")
    with patch.object(QuartzNetAligner, "_download_model") as download:
        monkeypatch.setenv("CTC_QUARTZNET_MODEL_PATH", str(folder))
        assert QuartzNetAligner()._model_path() == folder / "model.onnx"
        monkeypatch.setenv("CTC_QUARTZNET_MODEL_PATH", str(folder / "model.onnx"))
        assert QuartzNetAligner()._model_path() == folder / "model.onnx"
    download.assert_not_called()


def test_a_verified_cached_model_is_reused(data_dir, monkeypatch):
    cached = data_dir / "models" / "quartznet15x5-en" / "model.onnx"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"x" * 10)
    monkeypatch.setattr(qn, "_MODEL_BYTES", 10)
    with patch.object(QuartzNetAligner, "_download_model") as download:
        assert QuartzNetAligner()._model_path() == cached
    download.assert_not_called()


def _fake_get(payload):
    response = MagicMock()
    response.iter_content.return_value = [payload[:4], payload[4:]]
    response.__enter__.return_value = response
    return MagicMock(return_value=response)


def test_the_model_downloads_once_and_is_verified(data_dir, monkeypatch):
    payload = b"quartznet-bytes"
    monkeypatch.setattr(qn, "_MODEL_BYTES", len(payload))
    monkeypatch.setattr(qn, "_MODEL_SHA256", hashlib.sha256(payload).hexdigest())
    with patch("requests.get", _fake_get(payload)) as get:
        path = QuartzNetAligner()._model_path()
        assert QuartzNetAligner()._model_path() == path
    assert get.call_count == 1
    assert path.read_bytes() == payload
    assert not list(path.parent.glob("*.partial"))


def test_a_download_that_fails_verification_leaves_nothing_behind(data_dir, monkeypatch):
    monkeypatch.setattr(qn, "_MODEL_BYTES", 15)
    monkeypatch.setattr(qn, "_MODEL_SHA256", "0" * 64)
    with patch("requests.get", _fake_get(b"quartznet-bytes")):
        with pytest.raises(ValueError, match="failed verification"):
            QuartzNetAligner()._model_path()
    assert not list((data_dir / "models" / "quartznet15x5-en").iterdir())


@pytest.mark.parametrize("value, expected", [
    (None, QuartzNetAligner), ("quartznet", QuartzNetAligner),
    ("mms_fa", ForcedAligner), ("MMS", ForcedAligner),
])
def test_ctc_model_picks_the_aligner_per_call(value, expected, monkeypatch):
    if value is None:
        monkeypatch.delenv("CTC_MODEL", raising=False)
    else:
        monkeypatch.setenv("CTC_MODEL", value)
    service = AlignmentService(MagicMock(), MagicMock())
    assert service._ctc_aligner_class() is expected


def test_chapter_search_accepts_a_numpy_emission(monkeypatch):
    """_search_prior sees the same argmax sequence from numpy as from a torch tensor."""
    torch = pytest.importorskip("torch")
    from tests import test_ctc_chapter_search_prior as prior

    full, chapters = prior._book([3000, 3000])
    emission, _first = prior._emission(full, chapters, [0, 1])
    service = AlignmentService(MagicMock(), MagicMock())
    service._forced_aligner = MagicMock()
    service._forced_aligner.ctc_vocab.return_value = (0, prior.ID_TO_CHAR)

    from_torch = service._search_prior("book-1", (emission, prior.SPF), full, chapters)
    from_numpy = service._search_prior("book-1", (emission.numpy(), prior.SPF), full, chapters)

    assert from_torch is not None
    assert from_numpy == from_torch


def _spoken_book(words, gap=6):
    """One-hot emission speaking ``words`` in order; returns (text, log_probs, word start frames)."""
    text = " ".join(words)
    ids, starts = [], []
    for word in words:
        starts.append(len(ids))
        for ch in word:
            ids += [qn._VOCAB.index(ch) + 1, BLANK, BLANK]
        ids += [BLANK] * gap
    # -12.5, not -12: four wildcard frames (-3 each) would tie one off-target frame.
    log_probs = np.full((1, len(ids), 29), -12.5, dtype=np.float32)
    log_probs[0, np.arange(len(ids)), ids] = 0.0
    return text, log_probs, starts


def test_anchor_segments_absorb_an_imprecise_prior():
    """Anchors every ~10 words, each 1 s late (a transcript-derived map's error):
    the segment margin and edge wildcards still land the words on their frames.

    The wildcard costs -3 on every frame, silence included, so a segment's first
    letter may take the same letter in a neighbouring word inside the margin; one
    of these 120 words does. Nothing moves further than the margin."""
    import re

    aligner = _aligner_with_fake_session()
    rng = np.random.default_rng(9)
    words = ["".join(rng.choice(list("abcdefghijklmnopqrstuvwxyz"), size=int(rng.integers(3, 8))))
             for _ in range(120)]
    text, log_probs, starts = _spoken_book(words)
    offsets = [m.start() for m in re.finditer(r"\S+", text)]
    spf = 0.02
    boundaries = [{"char": offsets[k], "ts": starts[k] * spf + 1.0} for k in range(0, len(words), 10)]
    boundaries.append({"char": len(text), "ts": log_probs.shape[1] * spf})
    kept = [(w, o) for w, o in zip(words, offsets)]
    tokens = [[qn._VOCAB.index(c) + 1 for c in w] for w in words]

    with patch.object(QuartzNetAligner, "_MIN_SEGMENT_TOKENS", 30):
        result = dict(aligner._chunked_word_times(None, None, log_probs, kept, tokens, spf, boundaries))

    assert len(result) == len(words)
    errors = [abs(result[offset] - max(0.0, frame * spf - 0.28)) for offset, frame in zip(offsets, starts)]
    assert sum(e < 1e-6 for e in errors) >= len(words) - 2
    assert max(errors) <= QuartzNetAligner._SEGMENT_MARGIN_SECONDS


def _words(seed, count):
    rng = np.random.default_rng(seed)
    return ["".join(rng.choice(list("abcdefghijklmnopqrstuvwxyz"), size=int(rng.integers(3, 8))))
            for _ in range(count)]


def _gap_book():
    """40 words, then 30 s of other speech, then 40 more; anchors only at both ends
    of the gap (as the chapter search leaves them around non-text audio)."""
    import re

    first, second = _words(21, 40), _words(22, 40)
    text_a, lp_a, starts_a = _spoken_book(first)
    _t, lp_other, _s = _spoken_book(_words(23, 70))
    text_b, lp_b, starts_b = _spoken_book(second)
    log_probs = np.concatenate([lp_a, lp_other[:, :1500], lp_b], axis=1)
    offset_b = lp_a.shape[1] + 1500
    words = first + second
    text = text_a + " " + text_b
    offsets = [m.start() for m in re.finditer(r"\S+", text)]
    starts = starts_a + [offset_b + s for s in starts_b]
    spf = 0.02
    boundaries = [{"char": 0, "ts": 0.0}, {"char": offsets[40], "ts": starts[40] * spf},
                  {"char": len(text), "ts": log_probs.shape[1] * spf}]
    kept = list(zip(words, offsets))
    tokens = [[qn._VOCAB.index(c) + 1 for c in w] for w in words]
    return log_probs, kept, tokens, starts, boundaries, spf


def test_a_long_anchor_gap_is_never_split_at_an_interpolated_time():
    """Measured on Lolita: 2,121 chars sat between anchors 591 s apart, most of it
    non-text audio. Halving that segment at the linearly interpolated time put the
    second half 100-200 s late. The whole gap is one Viterbi pass instead."""
    aligner = _aligner_with_fake_session()
    log_probs, kept, tokens, starts, boundaries, spf = _gap_book()

    with patch.object(QuartzNetAligner, "_MAX_CHUNK_TOKENS", 60):
        result = dict(aligner._chunked_word_times(None, None, log_probs, kept, tokens, spf, boundaries))

    for (_word, offset), frame in zip(kept[:40], starts[:40]):
        assert result[offset] == pytest.approx(max(0.0, frame * spf - 0.28), abs=0.05), offset


def test_a_segment_too_big_for_one_pass_is_left_to_interpolation(caplog):
    aligner = _aligner_with_fake_session()
    log_probs, kept, tokens, starts, boundaries, spf = _gap_book()

    with patch.object(QuartzNetAligner, "_MAX_SEGMENT_CELLS", 10):
        result = dict(aligner._chunked_word_times(None, None, log_probs, kept, tokens, spf, boundaries))

    assert result == {}
    assert "interpolated between the anchors" in caplog.text


_ENGLISH = ("It was the best of times and it was the worst of times, and she had never seen "
            "a winter like it in all the years that she had lived by the sea. ") * 40
_FRENCH = ("Il était une fois, dans un petit village au bord de la mer, une jeune femme qui "
           "rêvait de partir loin. Elle regardait les bateaux chaque matin. ") * 40


def test_english_share_separates_english_from_other_languages():
    assert AlignmentService._english_share(_ENGLISH) > 0.3
    assert AlignmentService._english_share(_FRENCH) < 0.05
    assert AlignmentService._english_share("") == 0.0


@pytest.fixture
def real_service(tmp_path):
    from src.db.database_service import DatabaseService
    from src.utils.polisher import Polisher

    db = DatabaseService(str(tmp_path / "lang.db"))
    try:
        yield AlignmentService(db, Polisher())
    finally:
        db.db_manager.close()


@pytest.mark.parametrize("model, text, reaches_aligner", [
    ("quartznet", _FRENCH, False),
    ("quartznet", _ENGLISH, True),
    ("mms_fa", _FRENCH, True),
])
def test_quartznet_only_aligns_english_books(real_service, monkeypatch, caplog, model, text, reaches_aligner):
    """QuartzNet knows only English: a book that does not read as English goes to
    the transcription pipeline before the model is loaded. MMS is multilingual."""
    monkeypatch.setenv("CTC_MODEL", model)
    fake_map = [{"char": 0, "ts": 0.0}, {"char": len(text), "ts": 60.0}]
    with patch.object(QuartzNetAligner, "is_available", return_value=True), \
         patch.object(ForcedAligner, "is_available", return_value=True), \
         patch.object(ForcedAligner, "can_single_pass", return_value=True), \
         patch.object(ForcedAligner, "align", autospec=True, return_value=fake_map) as align:
        real_service.align_forced_and_store("book-1", ["/a.m4b"], text, audio_duration=60.0)

    assert align.called is reaches_aligner
    assert ("does not read as English" in caplog.text) is (not reaches_aligner)


def _sine(tmp_path, name, seconds, frequency):
    import subprocess

    path = tmp_path / name
    subprocess.run(["ffmpeg", "-y", "-nostdin", "-loglevel", "error", "-f", "lavfi",
                    "-i", f"sine=frequency={frequency}:sample_rate=16000:duration={seconds}",
                    "-c:a", "pcm_s16le", str(path)], check=True)
    return path


@pytest.mark.parametrize("returncode", [0, 1])
def test_pcm_windows_completes_when_decoder_stderr_exceeds_pipe_capacity(monkeypatch, returncode):
    """Replay #467 with a real child writing errors before it can produce PCM."""
    import subprocess
    import sys
    import threading

    popen = subprocess.Popen
    processes = []
    timers = []
    timed_out = threading.Event()
    samples = np.arange(7, dtype="<f4")
    diagnostic = "Header missing\nError while decoding MPEG audio frame\n"
    script = (
        "import sys; "
        f"sys.stderr.buffer.write({diagnostic.encode()!r} * 40000); "
        "sys.stderr.buffer.flush(); "
        f"sys.stdout.buffer.write({samples.tobytes()!r}); "
        f"sys.stdout.buffer.flush(); sys.exit({returncode})"
    )

    def start_decoder(command, **kwargs):
        assert command[0] == "ffmpeg"
        proc = popen([sys.executable, "-u", "-c", script], **kwargs)
        processes.append(proc)

        def stop_hung_decoder():
            if proc.poll() is None:
                timed_out.set()
                proc.kill()

        timer = threading.Timer(5, stop_hung_decoder)
        timers.append(timer)
        timer.start()
        return proc

    monkeypatch.setattr(subprocess, "Popen", start_decoder)
    monkeypatch.setattr(qn, "_WINDOW_SAMPLES", 4)
    # The container-timeline header probe is its own subprocess; this replays the decoder pipe only.
    monkeypatch.setattr(qn, "container_timeline_filter", lambda path: None)
    try:
        if returncode:
            with pytest.raises(RuntimeError, match="ffmpeg could not decode damaged.mp3") as error:
                list(QuartzNetAligner._pcm_windows("damaged.mp3"))
            assert str(error.value).endswith(diagnostic.strip())
            assert len(str(error.value)) < 400
        else:
            windows = list(QuartzNetAligner._pcm_windows(["damaged.mp3", "second.mp3"]))
            assert [len(window) for window in windows] == [4, 4, 4, 2]
            np.testing.assert_array_equal(np.concatenate(windows), np.tile(samples, 2))
    finally:
        for timer in timers:
            timer.cancel()
            timer.join()
        for proc in processes:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
        assert not timed_out.is_set(), "Decoder deadlocked on an unread stderr pipe (#467)"


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not on PATH -- decodes real audio")
def test_audio_streams_window_by_window_with_no_temp_file(tmp_path):
    """Two parts decode to exactly the samples the whole-book temp-file decode gives,
    in 38 s windows across the part boundary, without a whole-book audio temp file."""
    parts = [_sine(tmp_path, "a.wav", 25, 440), _sine(tmp_path, "b.wav", 25, 660)]
    whole = np.array(QuartzNetAligner()._decode_to_memmap(parts))

    windows = list(QuartzNetAligner._pcm_windows(parts))

    assert [w.shape[0] for w in windows] == [qn._WINDOW_SAMPLES, whole.shape[0] - qn._WINDOW_SAMPLES]
    np.testing.assert_array_equal(np.concatenate(windows), whole)

    aligner = _aligner_with_fake_session()
    with patch.object(QuartzNetAligner, "is_available", return_value=True), \
         patch.object(QuartzNetAligner, "_decode_to_memmap", side_effect=AssertionError("temp file")):
        emission, spf = aligner.emissions_for(parts)
    np.testing.assert_array_equal(emission, aligner._emissions(whole[None, :]))
    assert spf == pytest.approx(whole.shape[0] / emission.shape[1] / 16000)


def test_a_failed_model_download_says_how_to_supply_the_model(data_dir, caplog):
    with patch.object(QuartzNetAligner, "_download_model", side_effect=OSError("network is unreachable")):
        with pytest.raises(OSError):
            QuartzNetAligner()._load()
    assert "QuartzNet model file" in caplog.text
    assert "network is unreachable" in caplog.text


def test_align_runs_the_chunked_path_on_a_numpy_emission():
    """End to end through ForcedAligner's chunking, with no torch involved."""
    aligner = _aligner_with_fake_session()
    words = ["alpha", "beta", "gamma", "delta"] * 30
    text = " ".join(words)
    ids, starts = [], []
    for word in words:
        starts.append(len(ids))
        for ch in word:
            ids += [qn._VOCAB.index(ch) + 1, BLANK, BLANK]
        ids += [BLANK] * 6
    log_probs = np.full((1, len(ids), 29), -12.0, dtype=np.float32)
    log_probs[0, np.arange(len(ids)), ids] = 0.0
    spf = 0.02
    boundaries = [{"char": 0, "ts": 0.0}, {"char": len(text), "ts": len(ids) * spf}]

    with patch.object(QuartzNetAligner, "_MAX_CHUNK_TOKENS", 60), \
         patch.object(QuartzNetAligner, "is_available", return_value=True), \
         patch.object(QuartzNetAligner, "_load"):
        result = aligner.align("/fake.m4b", text, boundaries=boundaries, precomputed=(log_probs, spf))

    assert result is not None
    offsets = [m.start() for m in __import__("re").finditer(r"\S+", text)]
    by_char = {p["char"]: p["ts"] for p in result}
    for offset, frame in zip(offsets, starts):
        assert by_char[offset] == pytest.approx(max(0.0, frame * spf - 0.28), abs=1e-3), offset
