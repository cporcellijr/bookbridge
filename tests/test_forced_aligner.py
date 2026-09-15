"""Issue #426 Phase B: CTC forced aligner (torchaudio MMS_FA).

Pure helpers are tested directly. The full ``align()`` orchestration is exercised
against the *real* torchaudio ``forced_align``/``merge_tokens`` using a synthetic
emission tensor (no 1 GB model download): only the model forward and audio decode
are stubbed, so frame→time conversion, per-word token grouping and map assembly
run for real. ``align_forced_and_store`` is tested for its store/fallback contract.
"""

import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import pytest

from src.db.database_service import DatabaseService
from src.services.alignment_service import AlignmentService
from src.utils.forced_aligner import ForcedAligner
from src.utils.polisher import Polisher


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #

def test_book_words_normalizes_to_mms_charset_with_offsets():
    text = "The Butcher's 3 Masquerade!!"
    words = ForcedAligner._book_words(text)
    # "3" drops entirely (no a-z'); punctuation stripped; offsets index into text.
    assert words == [("the", 0), ("butcher's", 4), ("masquerade", 16)]
    assert text[4:13] == "Butcher's"
    assert text[16:26] == "Masquerade"


def test_book_words_skips_empty_and_returns_offsets_into_full_text():
    text = "\U0001f4da 1984 alpha"
    words = ForcedAligner._book_words(text)
    assert words == [("alpha", text.index("alpha"))]


def test_build_map_adds_head_and_tail_anchors_and_rounds():
    text = "x" * 100
    entries = [("a", 10), ("b", 40)]
    starts = [1.23456, 5.0]
    amap = ForcedAligner._build_map(entries, starts, text)
    assert amap == [
        {"char": 0, "ts": 0.0},
        {"char": 10, "ts": 1.235},
        {"char": 40, "ts": 5.0},
        {"char": 100, "ts": 5.0},
    ]


def test_build_map_no_head_when_first_word_at_zero():
    text = "abc def"
    amap = ForcedAligner._build_map([("abc", 0), ("def", 4)], [0.0, 0.5], text)
    assert amap[0] == {"char": 0, "ts": 0.0}
    assert amap[-1] == {"char": len(text), "ts": 0.5}
    # No duplicate leading (0,0) anchor.
    assert [a["char"] for a in amap] == [0, 4, 7]


def test_build_map_rejects_mismatched_lengths():
    assert ForcedAligner._build_map([("a", 0)], [], "abc") == []


def test_is_available_true_when_torch_present():
    pytest.importorskip("torchaudio")
    assert ForcedAligner.is_available() is True


# --------------------------------------------------------------------------- #
# Real forced_align math via a synthetic emission (no model download)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("device", ["cpu", "cuda"])
@pytest.mark.parametrize("with_bonus", [False, True])
def test_align_end_to_end_with_synthetic_emission(device, with_bonus, caplog):
    torch = pytest.importorskip("torch")
    torchaudio = pytest.importorskip("torchaudio")
    import torch.nn.functional as NN
    if device == "cuda" and not torch.cuda.is_available():
        pytest.skip("CUDA not available")

    # No adjacent repeated letters, so CTC needs no extra blank frames.
    full_text = "alpha beta delta"
    dictionary = torchaudio.pipelines.MMS_FA.get_dict()

    flat_tokens = []
    for word, _off in ForcedAligner._book_words(full_text):
        flat_tokens.extend(dictionary[c] for c in word)
    # Two emission frames per target token gives CTC slack and pins each token's
    # start to a known frame (token i -> frame 2i).
    per = 2
    n_frames = len(flat_tokens) * per  # 14 tokens * 2 = 28
    vocab = len(dictionary)
    emis = torch.full((1, n_frames, vocab), -10.0)
    for i, tok in enumerate(flat_tokens):
        emis[0, i * per, tok] = 0.0
        emis[0, i * per + 1, tok] = 0.0
    emission = NN.log_softmax(emis, dim=-1).to(device)

    aligner = ForcedAligner()
    frame_cursor = 0

    def fake_model(piece):
        nonlocal frame_cursor
        assert piece.device.type == device
        count = piece.size(1) // 1600
        chunk = emission[:, frame_cursor:frame_cursor + count]
        frame_cursor += count
        return chunk, None

    def fake_load(self):
        self._model = fake_model
        self._dict = dictionary
        self._device = device
        self._sample_rate = 16000

    # 1600 samples/frame @ 16 kHz => 0.1 s per frame, so word starts are exact.
    waveform = torch.zeros(1, n_frames * 1600)
    text_range = None
    if with_bonus:
        full_text = "Intro " + full_text + " Bonus excerpt never narrated"
        text_range = (6, 22)

    with caplog.at_level("INFO"), \
         patch.object(ForcedAligner, "_load", fake_load), \
         patch.object(ForcedAligner, "_load_audio", return_value=waveform), \
         patch("src.utils.forced_aligner._EMIT_WINDOW_SECONDS", 1), \
         patch.object(torchaudio.functional, "forced_align", wraps=torchaudio.functional.forced_align) as forced, \
         patch.object(torchaudio.functional, "merge_tokens", wraps=torchaudio.functional.merge_tokens) as merge:
        amap = aligner.align(["/fake/audio.m4b"], full_text, text_range=text_range)

    assert frame_cursor == n_frames  # Real multi-window emission concatenation.
    assert forced.call_args.args[0].device.type == device
    assert forced.call_args.args[1].device.type == device
    assert all(t.device.type == "cpu" for t in merge.call_args.args)
    assert f"CTC: forced_align on {device}" in caplog.text
    # alpha token0->frame0 (0.0s); beta token5->frame10 (1.0s); delta token9->frame18 (1.8s)
    expected = [
        {"char": 0, "ts": 0.0},
        {"char": 6, "ts": 1.0},
        {"char": 11, "ts": 1.8},
        {"char": 16, "ts": 1.8},
    ]
    if with_bonus:
        expected = [{"char": 0, "ts": 0.0}] + [
            {"char": point["char"] + 6, "ts": point["ts"]} for point in expected
        ]
    assert amap == expected


def test_align_returns_none_when_no_alignable_words():
    aligner = ForcedAligner()
    assert aligner.align(["/fake.m4b"], "1984 —— \U0001f4da") is None


def test_chunked_align_covers_whole_book_via_boundaries():
    """A book too big for one pass is aligned in lexical-map-bounded chunks."""
    torch = pytest.importorskip("torch")
    torchaudio = pytest.importorskip("torchaudio")
    import torch.nn.functional as NN

    letters = list("abcdefghijklmnopqrstuvwxyz")
    full_text = " ".join(letters)          # word i at char 2*i; len 51
    d = torchaudio.pipelines.MMS_FA.get_dict()
    per = 4
    n_frames = len(letters) * per          # 104 frames; word i occupies frames [4i, 4i+3]
    emis = torch.full((1, n_frames, len(d)), -10.0)
    emis[0, :, 0] = -1.0                    # blank likely everywhere (as in a real emission)
    for i, ch in enumerate(letters):
        for f in range(per):
            emis[0, i * per + f, d[ch]] = 0.0
    emission = NN.log_softmax(emis, dim=-1)

    aligner = ForcedAligner()
    cursor = 0

    def fake_model(piece):
        nonlocal cursor
        cnt = piece.size(1) // 1600
        chunk = emission[:, cursor:cursor + cnt]
        cursor += cnt
        return chunk, None

    def fake_load(self):
        self._model = fake_model
        self._dict = d
        self._device = "cpu"
        self._sample_rate = 16000

    waveform = torch.zeros(1, n_frames * 1600)   # 0.1 s/frame
    boundaries = [{"char": 0, "ts": 0.0}, {"char": len(full_text), "ts": n_frames * 0.1}]

    with patch.object(ForcedAligner, "_load", fake_load), \
         patch.object(ForcedAligner, "_load_audio", return_value=waveform), \
         patch("src.utils.forced_aligner._EMIT_WINDOW_SECONDS", 1), \
         patch.object(ForcedAligner, "_single_pass_fits", return_value=False), \
         patch.object(ForcedAligner, "_MAX_CHUNK_TOKENS", 8), \
         patch.object(ForcedAligner, "_CHUNK_MARGIN_SECONDS", 0.6):
        amap = aligner.align(["/a.m4b"], full_text, boundaries=boundaries)

    assert amap is not None
    assert cursor == n_frames                          # emissions built once over the whole audio
    ts = [p["ts"] for p in amap]
    assert ts == sorted(ts)                            # monotonic across chunk seams
    got = {p["char"]: p["ts"] for p in amap}
    # Every word covered, each near its true start (frame 4i -> 0.4i s), within a
    # small tolerance for chunk-boundary slack.
    assert set(2 * i for i in range(len(letters))) <= set(got)
    for i in range(len(letters)):
        assert abs(got[2 * i] - 0.4 * i) <= 0.25, (i, got[2 * i])


@pytest.mark.parametrize("excluded", [None, [], [(9999, 10000)], "interior"])
@pytest.mark.parametrize("max_tokens", [8, 1000])
def test_exclusions_split_targets_and_preserve_exact_narrated_frames(excluded, max_tokens):
    """Unspoken paragraphs never enter forced_align; empty exclusions are identical."""
    import json

    torch = pytest.importorskip("torch")
    torchaudio = pytest.importorskip("torchaudio")
    letters = list("abcdefghijklmnopqrstuvwxyz")
    dictionary = torchaudio.pipelines.MMS_FA.get_dict()
    emission = torch.full((1, 104, len(dictionary)), -10.0)
    emission[0, :, 0] = -1.0
    for i, letter in enumerate(letters):
        emission[0, i * 4:(i + 1) * 4, dictionary[letter]] = 0.0
    emission = torch.log_softmax(emission, dim=-1)
    aligner = ForcedAligner()
    aligner._dict = dictionary
    aligner._device = "cpu"
    text = " ".join(letters)
    spans = excluded
    if excluded == "interior":
        gap = "note " * 400
        text = text[:16] + gap + text[16:36] + gap + text[36:]
        # Deliberately unsorted and overlapping to exercise normalization.
        spans = [(2036, 4036), (17, 2000), (16, 2016)]
    entries = aligner._book_words(text)
    narrated = [(word, char) for word, char in entries if word in letters]
    boundaries = [{"char": char, "ts": i * 0.4} for i, (_word, char) in enumerate(narrated)]
    boundaries.append({"char": len(text), "ts": 10.4})
    with patch.object(aligner, "_load"), \
         patch.object(aligner, "_load_audio", return_value=torch.zeros(1, 104 * 1600)), \
         patch.object(aligner, "_emissions", return_value=emission), \
         patch.object(aligner, "_MAX_CHUNK_TOKENS", max_tokens), \
         patch.object(torchaudio.functional, "forced_align", wraps=torchaudio.functional.forced_align) as forced:
        result = aligner.align("/fake.m4b", text, boundaries=boundaries, exclude_spans=spans)
    expected = [{"char": char, "ts": round(i * 0.4, 3)} for i, (_word, char) in enumerate(narrated)]
    expected.append({"char": len(text), "ts": 10.0})
    assert json.dumps(result) == json.dumps(expected)
    if excluded == "interior":
        assert forced.call_count >= 3
        got_tokens = []
        for call in forced.call_args_list:
            targets = call.args[1][0].tolist()
            got_tokens += targets
            assert not (dictionary["h"] in targets and dictionary["i"] in targets)
            assert not (dictionary["r"] in targets and dictionary["s"] in targets)
            # The 15s audio margins must also stop at the gap, otherwise real
            # speech in the overlap drags the last left words past the seam.
            segment = call.args[0]
            if targets[0] == dictionary['a']:
                assert torch.equal(segment, emission[:, :32])
            if targets[0] == dictionary['i']:
                assert torch.equal(segment, emission[:, 32:72])
        assert got_tokens == [dictionary[letter] for letter in letters]
        for lo, hi in [(16, 2016), (2036, 4036)]:
            assert not any(lo <= p["char"] < hi for p in result)
            left = ForcedAligner._interp_ts(result, lo)
            middle = ForcedAligner._interp_ts(result, (lo + hi) // 2)
            right = ForcedAligner._interp_ts(result, hi)
            assert left <= middle <= right
    else:
        assert forced.call_count == 1


def test_exclusions_without_boundaries_fail_before_loading_audio():
    aligner = ForcedAligner()
    with patch.object(aligner, "is_available", return_value=True), \
         patch.object(aligner, "_load_audio") as decode:
        assert aligner.align("/fake.m4b", "alpha note beta", exclude_spans=[(6, 11)]) is None
    decode.assert_not_called()


def test_full_book_cpu_alignment_falls_back_before_native_overflow(caplog):
    torch = pytest.importorskip("torch")
    torchaudio = pytest.importorskip("torchaudio")
    aligner = ForcedAligner()
    aligner._dict = {"a": 1}
    aligner._device = "cpu"
    emission = MagicMock()
    emission.size.return_value = 111000
    emission.device = torch.device("cpu")
    waveform = MagicMock()
    waveform.size.return_value = 2220 * 16000
    with patch.object(aligner, "_load"), \
         patch.object(aligner, "_load_audio", return_value=waveform), \
         patch.object(aligner, "_emissions", return_value=emission) as emissions, \
         patch.object(torchaudio.functional, "forced_align") as forced:
        assert aligner.align("/full-book.m4b", "a " * 92000) is None
    forced.assert_not_called()
    # The doomed pass is rejected from the decoded sample count (~320 samples/frame),
    # before the expensive emissions forward — so a new long book wastes only a decode.
    emissions.assert_not_called()
    assert "CPU alignment exceeds the safe back-pointer limit" in caplog.text


def test_load_audio_decodes_to_mono_16k_via_ffmpeg(tmp_path):
    import shutil
    import subprocess
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not on PATH")
    pytest.importorskip("numpy")
    pytest.importorskip("torch")
    # Generate a 2s stereo 44.1 kHz tone; _load_audio must downmix + resample to 16k mono.
    wav = tmp_path / "tone.wav"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=2", "-ac", "2", "-ar", "44100", str(wav)],
        check=True,
    )
    aligner = ForcedAligner()
    aligner._sample_rate = 16000
    wf = aligner._load_audio([str(wav)])
    assert wf.dim() == 2 and wf.size(0) == 1          # mono
    assert abs(wf.size(1) - 16000 * 2) < 400          # ~2s at 16 kHz


# --------------------------------------------------------------------------- #
# align_forced_and_store: store + fallback contract
# --------------------------------------------------------------------------- #

@pytest.fixture
def service(tmp_path):
    db = DatabaseService(str(tmp_path / "ctc.db"))
    try:
        yield AlignmentService(db, Polisher())
    finally:
        db.db_manager.close()


def test_align_forced_and_store_persists_ctc_method(service):
    # A real CTC map is dense; a near-linear map (few points, one big gap) is now
    # rejected by the acceptance gate, so the store contract is tested with a dense one.
    fake_map = [{"char": c, "ts": c / 10.0} for c in range(0, 101, 10)]
    with patch("src.utils.forced_aligner.ForcedAligner.is_available", return_value=True), \
         patch("src.utils.forced_aligner.ForcedAligner.align", return_value=fake_map):
        ok = service.align_forced_and_store("ctc-book", ["/a.m4b"], "x" * 100)
    assert ok is True
    assert service.database_service.get_alignment_method("ctc-book") == "ctc"
    assert service.database_service.get_alignment_total_chars("ctc-book") == 100


@pytest.mark.parametrize("coverage,recorded_chars,trim", [(96, 1000, True), (60, 1000, False), (96, 999, False)])
def test_remap_uses_narrated_chapters_without_bonus_excerpt(service, coverage, recorded_chars, trim):
    text = "x" * 1000
    previous = [
        {"char": 0, "ts": 0.0},
        {"char": 15, "ts": 2.0, "t_idx": 1},
        {"char": 45, "ts": 2.0 + coverage, "t_idx": 50},
        {"char": recorded_chars, "ts": 100.0},
    ]
    service._save_alignment("bonus-book", previous, "lexical", total_chars=recorded_chars)
    chapters = [{"start": 0, "end": 9}, {"start": 10, "end": 50}, {"start": 51, "end": 1000}]
    # Dense enough to clear the acceptance gate; still spans chars 10..50 at ts 2..98.
    fake_map = [{"char": c, "ts": 2.0 + (c - 10) * 2.4} for c in range(10, 51, 8)]
    with patch.object(ForcedAligner, "is_available", return_value=True), \
         patch.object(ForcedAligner, "align", return_value=fake_map) as align:
        assert service.align_forced_and_store("bonus-book", ["/a.m4b"], text, chapters)
    align.assert_called_once()
    call_args, call_kwargs = align.call_args
    assert call_args == (["/a.m4b"], text)
    assert call_kwargs["text_range"] == ((10, 50) if trim else None)
    # The prior lexical map is handed over as chunk boundaries only when it was built
    # against this exact text length (so the char spaces line up).
    assert (call_kwargs.get("boundaries") is not None) == (recorded_chars == 1000)
    assert service.database_service.get_alignment_total_chars("bonus-book") == 1000
    assert service.get_char_for_time("bonus-book", 99) == 50


def test_align_forced_and_store_fallback_when_unavailable(service):
    with patch("src.utils.forced_aligner.ForcedAligner.is_available", return_value=False):
        ok = service.align_forced_and_store("no-ctc", ["/a.m4b"], "x" * 100)
    assert ok is False
    assert service.database_service.get_alignment_method("no-ctc") is None


def test_align_forced_and_store_fallback_on_empty_map(service):
    with patch("src.utils.forced_aligner.ForcedAligner.is_available", return_value=True), \
         patch("src.utils.forced_aligner.ForcedAligner.align", return_value=None):
        ok = service.align_forced_and_store("empty", ["/a.m4b"], "x" * 100)
    assert ok is False
    assert service.database_service.get_alignment_method("empty") is None


def test_align_forced_and_store_rejects_empty_text(service):
    ok = service.align_forced_and_store("notext", ["/a.m4b"], "")
    assert ok is False


def test_ctc_enabled_reads_env():
    os.environ.pop("CTC_ENABLED", None)
    assert AlignmentService.ctc_enabled() is False
    os.environ["CTC_ENABLED"] = "true"
    try:
        assert AlignmentService.ctc_enabled() is True
    finally:
        os.environ.pop("CTC_ENABLED", None)


@pytest.fixture
def recovery_book():
    """Real CTC emissions, with lexical compression that provably skips two chunks."""
    torch = pytest.importorskip("torch")
    torchaudio = pytest.importorskip("torchaudio")
    letters = list("abcdefghijklmnopqrstuvwxyz")
    dictionary = torchaudio.pipelines.MMS_FA.get_dict()
    emission = torch.full((1, 104, len(dictionary)), -10.0)
    emission[0, :, 0] = -1.0
    for i, letter in enumerate(letters):
        emission[0, i * 4:(i + 1) * 4, dictionary[letter]] = 0.0
    emission = torch.log_softmax(emission, dim=-1)

    def run(compressed=True, exclusion_at=None, recover=True):
        text = " ".join(letters)
        excluded = []
        if exclusion_at is not None:
            char = 2 * exclusion_at
            text = text[:char] + "note " * 40 + text[char:]
            excluded = [(char, char + 200)]
        aligner = ForcedAligner()
        aligner._dict = dictionary
        aligner._device = "cpu"
        entries = [(word, char) for word, char in aligner._book_words(text) if word in letters]
        boundaries = [{"char": char, "ts": i * 0.4}
                      for i, (_word, char) in enumerate(entries)]
        if compressed:
            for i in range(8, 20):
                boundaries[i]["ts"] = 3.2 + (i - 8) * 0.001
        # Exclusion edges have known audio onsets, independent of compressed chunks.
        if exclusion_at is not None:
            boundaries[exclusion_at]["ts"] = exclusion_at * 0.4
        expected = [{"char": char, "ts": round(i * 0.4, 3)}
                    for i, (_word, char) in enumerate(entries)]
        expected.append({"char": len(text), "ts": 10.0})
        original = aligner._recover_skipped_spans
        missing = []
        calls = []

        def recovery(*args):
            missing.append(sum(ts is None for ts in args[7]))
            return original(*args) if recover else args[7]

        def segment(F, torch_module, part, targets, spf, frame_offset=0):
            calls.append((len(targets), part.size(1), frame_offset))
            return ForcedAligner._segment_word_times(
                aligner, F, torch_module, part, targets, spf, frame_offset)

        with patch.object(aligner, "_load"), \
             patch.object(aligner, "_load_audio", return_value=torch.zeros(1, 104 * 1600)), \
             patch.object(aligner, "_emissions", return_value=emission), \
             patch.object(aligner, "_single_pass_fits", side_effect=lambda d, f, t: t <= 4), \
             patch.object(aligner, "_MAX_CHUNK_TOKENS", 4), \
             patch.object(aligner, "_CHUNK_MARGIN_SECONDS", 0.1), \
             patch.object(aligner, "_recover_skipped_spans", side_effect=recovery), \
             patch.object(aligner, "_segment_word_times", side_effect=segment):
            result = aligner.align("/fake.m4b", text, boundaries=boundaries, exclude_spans=excluded)
        return result, expected, missing, calls, excluded

    return run


def test_recovery_recovers_compressed_region_via_bracketing_anchors(recovery_book, caplog):
    """Skipped middle letters recover to the same true frames as a clean map."""
    result, expected, missing, calls, _ = recovery_book()
    clean, *_ = recovery_book(compressed=False)
    assert missing == [8]
    assert result == expected == clean
    assert max(tokens for tokens, _frames, _offset in calls) <= 4
    # The final two native calls recover separate four-word chunks.
    assert len(calls) == 7
    with caplog.at_level("INFO"):
        recovery_book()
    assert "recovery aligned 8 words; 0 words remain interpolated" in caplog.text


def test_recovery_noop_when_no_skips(recovery_book):
    """No-skip chunked maps remain byte-identical to Pass 1."""
    import json
    result, expected, missing, calls, _ = recovery_book(compressed=False)
    first_pass, *_ = recovery_book(compressed=False, recover=False)
    assert missing == [0]
    assert len(calls) == 7
    assert json.dumps(result) == json.dumps(first_pass) == json.dumps(expected)


@pytest.mark.parametrize("exclusion_at", [4, 12])
def test_recovery_preserves_exclusions_and_recovers_compression(recovery_book, exclusion_at):
    result, expected, missing, calls, excluded = recovery_book(exclusion_at=exclusion_at)
    assert missing[0] > 0
    assert result == expected
    lo, hi = excluded[0]
    assert not any(lo <= point["char"] < hi for point in result)
    edge_frame = exclusion_at * 4
    # No audio window, including recovery, may straddle the exclusion boundary.
    assert all(offset + frames <= edge_frame or offset >= edge_frame
               for _tokens, frames, offset in calls)


@pytest.mark.parametrize("times", [[0.0, None, None, 2.0], [None, 1.0, 2.0, 3.0],
                                   [0.0, 1.0, 2.0, None]])
def test_recovery_leaves_no_audio_and_unbracketed_runs_unaligned(times):
    torch = pytest.importorskip("torch")
    aligner = ForcedAligner()
    before = times[:]
    with patch.object(aligner, "_segment_word_times") as segment:
        aligner._recover_skipped_spans(None, torch, torch.zeros(1, 10, 2),
                                      [("a", 0), ("b", 2), ("c", 4), ("d", 6)],
                                      [[1]] * 4, 1.0, [], times, 10, [])
    segment.assert_not_called()
    assert times == before


def test_recovery_splits_for_memory_and_caps_total_work():
    torch = pytest.importorskip("torch")
    aligner = ForcedAligner()
    kept = [(str(i), i * 2) for i in range(12)]
    tokens = [[1]] * 12
    emission = torch.zeros(1, 100, 2)

    def segment(F, torch_module, part, targets, spf, frame_offset=0):
        return [float(frame_offset + i) for i in range(len(targets))]

    with patch.object(aligner, "_MAX_CHUNK_TOKENS", 4), \
         patch.object(aligner, "_single_pass_fits", side_effect=lambda d, f, t: t <= 2) as fits, \
         patch.object(aligner, "_segment_word_times", side_effect=segment) as native:
        aligner._recover_skipped_spans(None, torch, emission, kept, tokens, 1.0,
                                      [], [10.0] + [None] * 10 + [90.0], 100, [])
    assert fits.call_count > native.call_count > 1
    assert all(sum(map(len, call.args[3])) <= 2 for call in native.call_args_list)
    for fits_result, budget in [(False, 2**38), (True, 0)]:
        times = [10.0] + [None] * 10 + [90.0]
        with patch.object(aligner, "_single_pass_fits", return_value=fits_result), \
             patch.object(aligner, "_MAX_RECOVERY_CELLS", budget), \
             patch.object(aligner, "_segment_word_times") as native:
            aligner._recover_skipped_spans(None, torch, emission, kept, tokens, 1.0,
                                          [], times, 100, [])
        native.assert_not_called()
        assert times == [10.0] + [None] * 10 + [90.0]


def test_recovery_keeps_first_narrated_word_at_exclusion_endpoint():
    torch = pytest.importorskip("torch")
    aligner = ForcedAligner()
    times = [10.0, None, 90.0]
    with patch.object(aligner, "_segment_word_times", return_value=[50.0]) as segment:
        aligner._recover_skipped_spans(
            None, torch, torch.zeros(1, 100, 2), [("a", 0), ("b", 20), ("c", 22)],
            [[1]] * 3, 1.0, [{"char": 0, "ts": 10.0}, {"char": 20, "ts": 50.0}],
            times, 100, [(2, 20)])
    segment.assert_called_once()
    assert segment.call_args.kwargs["frame_offset"] == 50
    assert times == [10.0, 50.0, 90.0]


def test_recovery_preserves_bracketing_anchors_when_windows_overlap():
    torch = pytest.importorskip("torch")
    aligner = ForcedAligner()
    times = [10.0, None, None, None, None, 90.0]
    with patch.object(aligner, "_MAX_CHUNK_TOKENS", 2), \
         patch.object(aligner, "_segment_word_times", side_effect=[[11.0, 60.0], [50.0, 95.0]]):
        aligner._recover_skipped_spans(
            None, torch, torch.zeros(1, 100, 2), [("a", i * 2) for i in range(6)],
            [[1]] * 6, 1.0, [], times, 100, [])
    assert times == [10.0, 11.0, 60.0, None, None, 90.0]
