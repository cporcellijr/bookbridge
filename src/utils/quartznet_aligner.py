"""CTC forced alignment with NVIDIA's QuartzNet15x5 (English) on onnxruntime.

The torch-free counterpart of ``ForcedAligner``: it runs on CPU in the standard
image, where ``onnxruntime`` already ships as a faster-whisper dependency, so CTC
alignment no longer needs the multi-gigabyte ``-ctc`` image. The word bookkeeping,
pre-flight sizing and map assembly are ``ForcedAligner``'s; the audio is decoded as
a stream, and the text is aligned anchor to anchor (`_chunked_word_times`).

The model is the ONNX conversion Storyteller's ghost-story engine uses (18.9M
parameters, 77 MB). Its graph contains the mel frontend, so the caller supplies
only a pre-emphasised, reflect-padded 16 kHz signal; the output is log-probs over
space, ``a``-``z``, apostrophe and blank at one frame per 20 ms. The weights are
NVIDIA's (NGC Terms of Use), so BookBridge never bundles them: an admin may point
``CTC_QUARTZNET_MODEL_PATH`` at a copy (e.g. a Storyteller install's), otherwise the
file is downloaded once from Storyteller's public package registry into
``DATA_DIR/models`` and verified against a pinned size and SHA-256.

`ctc_viterbi_first_frames` is a numpy port of ``ctcViterbiAlign`` from
Storyteller's ``libraries/align/src/align/ctc/forcedAlign.ts``
(MIT, Copyright (c) 2023 Shane Friedman).
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from src.utils.forced_aligner import ForcedAligner, container_timeline_filter

logger = logging.getLogger(__name__)

_SAMPLE_RATE = 16000
_SAMPLES_PER_FRAME = 320
# ghost-story runs the model on 38 s windows; a full window yields one extra frame
# at its right edge (the same instant as the next window's first frame), which is
# dropped so every frame sits on one book-wide 20 ms grid.
_WINDOW_SAMPLES = 38 * _SAMPLE_RATE
_FRAMES_PER_WINDOW = _WINDOW_SAMPLES // _SAMPLES_PER_FRAME
# Half of the graph's 512-point FFT: the signal is centre-padded by this much.
_PAD_SAMPLES = 256
_MIN_WINDOW_SAMPLES = 4 * 512
_PRE_EMPHASIS = 0.97
_SPACE_ID = 0
_BLANK_ID = 28
# Alignment targets carry no spaces, so the space class is folded into blank.
_FOLDED_SPACE_SCORE = -30.0
_VOCAB = "abcdefghijklmnopqrstuvwxyz'"
# QuartzNet's character peaks trail the speech; ghost-story shifts starts by this.
# Measured against MMS evidence on three books, it lands 50-60 ms ahead of MMS,
# whose own onsets run 60-120 ms late.
_ONSET_OFFSET_SECONDS = -0.28
# ghost-story's STAR_SCORE: the per-frame log-prob of the wildcard label.
_STAR_SCORE = -3.0
_STAR_ID = 29

_MODEL_URL = "https://gitlab.com/api/v4/projects/67994333/packages/ml_models/3023100/files/model.onnx"
_MODEL_BYTES = 76711679
_MODEL_SHA256 = "3a4cf199c53475ae752694bb65989c05755bf6920d8ac8aaf51581a1021bdf2c"


def ctc_viterbi_first_frames(log_probs: Any, labels: Sequence[int], blank: int,
                             star: Optional[int] = None,
                             star_score: float = _STAR_SCORE) -> Optional[List[int]]:
    """Best CTC path of ``labels`` through ``log_probs`` ([frames, classes]).

    Returns the first frame of each label on that path, or ``None`` when no path
    exists (fewer frames than the labels need). The path may start on a leading
    blank or the first label and end on the last label or a trailing blank, the
    same boundary rule as torchaudio's ``forced_align``.

    ``star`` (an id past the last class) is ghost-story's wildcard: it scores
    ``star_score`` on every frame, so a star label absorbs speech that is not the
    target text. A star as the first or last label may also be skipped.
    """
    import numpy as np

    num_frames, num_labels = int(log_probs.shape[0]), len(labels)
    if not num_labels or not num_frames:
        return None
    if star is not None:
        log_probs = np.concatenate(
            [log_probs, np.full((num_frames, star + 1 - log_probs.shape[1]), star_score, dtype=log_probs.dtype)],
            axis=1,
        )
    states = 2 * num_labels + 1
    tokens = np.full(states, blank, dtype=np.int64)
    tokens[1::2] = labels
    skip_ok = np.zeros(states, dtype=bool)
    skip_ok[2:] = (tokens[2:] != tokens[:-2]) | (tokens[2:] == star)
    prev = np.full(states, -np.inf)
    prev[:2] = log_probs[0, tokens[:2]]
    if star is not None and labels[0] == star and states > 3:
        prev[2:4] = log_probs[0, tokens[2:4]]
    back = np.zeros((num_frames, states), dtype=np.uint8)
    step = np.full(states, -np.inf)
    jump = np.full(states, -np.inf)
    for t in range(1, num_frames):
        step[1:] = prev[:-1]
        jump[2:] = prev[:-2]
        jump[~skip_ok] = -np.inf
        best = np.maximum(prev, step)
        op = (step > prev).astype(np.uint8)
        take_jump = jump > best
        best[take_jump] = jump[take_jump]
        op[take_jump] = 2
        prev = best + log_probs[t, tokens]
        back[t] = op
    terminals = [states - 1, states - 2]
    if star is not None and labels[-1] == star and states > 3:
        terminals += [states - 3, states - 4]
    state = max(terminals, key=lambda s: prev[s])
    if prev[state] == -np.inf:
        return None
    first = np.full(states, -1, dtype=np.int64)
    for t in range(num_frames - 1, -1, -1):
        first[state] = t
        state -= int(back[t, state])
    return [int(f) for f in first[1::2]]


class QuartzNetAligner(ForcedAligner):
    """QuartzNet15x5 forced aligner on onnxruntime (see module docstring)."""

    # The numpy Viterbi keeps one back-pointer byte per frame x state.
    _MAX_SINGLE_PASS_CELLS = 2**26

    def __init__(self):
        super().__init__()
        self._session = None

    @staticmethod
    def is_available() -> bool:
        """True when onnxruntime and numpy are importable (the standard image)."""
        import importlib.util
        try:
            return bool(importlib.util.find_spec("onnxruntime") and importlib.util.find_spec("numpy"))
        except (ImportError, ValueError):
            return False

    @staticmethod
    def _cached_model_path() -> Path:
        return Path(os.environ.get("DATA_DIR", "/data")) / "models" / "quartznet15x5-en" / "model.onnx"

    def _model_path(self) -> Path:
        """The configured model file, else the verified cached download (fetched once)."""
        configured = os.environ.get("CTC_QUARTZNET_MODEL_PATH", "").strip()
        if configured:
            path = Path(configured)
            if path.is_dir():
                path = path / "model.onnx"
            if path.is_file():
                return path
            logger.warning(
                "⚠️ CTC: CTC_QUARTZNET_MODEL_PATH %s has no model.onnx; using the downloaded model",
                configured,
            )
        cached = self._cached_model_path()
        if cached.is_file() and cached.stat().st_size == _MODEL_BYTES:
            return cached
        self._download_model(cached)
        return cached

    @staticmethod
    def _download_model(dest: Path) -> None:
        import requests

        dest.parent.mkdir(parents=True, exist_ok=True)
        partial = dest.with_name(dest.name + ".partial")
        logger.info("⬇️ CTC: downloading the QuartzNet model (%.0f MB) to %s", _MODEL_BYTES / 1e6, dest)
        digest = hashlib.sha256()
        try:
            with requests.get(_MODEL_URL, stream=True, timeout=60) as resp:
                resp.raise_for_status()
                with open(partial, "wb") as out:
                    for chunk in resp.iter_content(1 << 20):
                        digest.update(chunk)
                        out.write(chunk)
            size = partial.stat().st_size
            if size != _MODEL_BYTES or digest.hexdigest() != _MODEL_SHA256:
                raise ValueError(
                    f"QuartzNet model download failed verification ({size} bytes, sha256 {digest.hexdigest()})"
                )
            os.replace(partial, dest)
        finally:
            if partial.exists():
                partial.unlink()

    def _load(self):
        """Load and cache the ONNX session (downloading the model on first use)."""
        if self._session is not None:
            return
        import onnxruntime as ort

        try:
            path = self._model_path()
        except Exception as e:
            logger.warning(
                "⚠️ CTC: could not get the QuartzNet model (%s); set 'QuartzNet model file' "
                "(CTC_QUARTZNET_MODEL_PATH) to a local model.onnx, such as a Storyteller "
                "install's ghost-story copy. Falling back to the transcription pipeline.",
                e, exc_info=True,
            )
            raise
        options = ort.SessionOptions()
        options.intra_op_num_threads = max(1, min(8, os.cpu_count() or 1))
        logger.info("⚙️ CTC: loading QuartzNet15x5 (onnxruntime, CPU) from %s", path)
        self._session = ort.InferenceSession(str(path), options, providers=["CPUExecutionProvider"])
        self._model = self._session
        self._device = "cpu"
        self._sample_rate = _SAMPLE_RATE
        self._dict = {char: index + 1 for index, char in enumerate(_VOCAB)}

    def _load_audio(self, audio_paths):
        return self._decode_to_memmap(audio_paths)[None, :]

    @staticmethod
    def _pcm_windows(audio_paths) -> Iterator[Any]:
        """Decode ``audio_paths`` in order through ffmpeg and yield consecutive
        ``_WINDOW_SAMPLES`` float32 windows (the last may be shorter).

        Streams, so at most one window of audio is held: the shared decode writes
        the whole book to a temp file first, ~230 MB per hour (5 GB for a 22-hour
        book), which a small NAS may not have to spare.
        """
        import subprocess
        import tempfile

        import numpy as np

        if isinstance(audio_paths, (str, os.PathLike)):
            audio_paths = [audio_paths]
        window_bytes = _WINDOW_SAMPLES * 4
        pending = bytearray()
        for path in audio_paths:
            # Decode errors must not fill an unread pipe while stdout is streamed.
            timeline = container_timeline_filter(path)
            with tempfile.TemporaryFile() as errors_file:
                proc = subprocess.Popen(
                    ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(path),
                     *(["-af", timeline] if timeline else []),
                     "-f", "f32le", "-ac", "1", "-ar", str(_SAMPLE_RATE), "pipe:1"],
                    stdout=subprocess.PIPE, stderr=errors_file,
                )
                try:
                    while True:
                        chunk = proc.stdout.read(window_bytes - len(pending))
                        if not chunk:
                            break
                        pending += chunk
                        if len(pending) == window_bytes:
                            yield np.frombuffer(bytes(pending), dtype="<f4")
                            pending.clear()
                finally:
                    proc.stdout.close()
                    returncode = proc.wait()
                if returncode != 0:
                    errors_file.seek(max(0, errors_file.seek(0, os.SEEK_END) - 4096))
                    errors = errors_file.read()
                    raise RuntimeError(
                        f"ffmpeg could not decode {path}: {errors.decode('utf-8', 'replace').strip()[-300:]}"
                    )
        if pending:
            yield np.frombuffer(bytes(pending), dtype="<f4")

    def _stream_emissions(self, audio_paths) -> Tuple[Any, float]:
        """``(emission [1, T, 29], seconds_per_frame)`` decoded window by window."""
        samples = 0

        def counted():
            nonlocal samples
            for window in self._pcm_windows(audio_paths):
                samples += window.shape[0]
                yield window

        emission = self._emissions_from_windows(counted())
        if not samples:
            raise ValueError("ffmpeg produced no audio samples")
        logger.info("⚙️ CTC: QuartzNet emissions for %.0fs audio (streamed, CPU)", samples / _SAMPLE_RATE)
        return emission, samples / emission.shape[1] / _SAMPLE_RATE

    def emissions_for(self, audio_paths) -> Optional[Tuple[Any, float]]:
        """Decode ``audio_paths`` and run the model once: ``(emission, seconds_per_frame)``."""
        if not self.is_available():
            return None
        try:
            self._load()
            return self._stream_emissions(audio_paths)
        except Exception as e:
            logger.error(f"❌ CTC emissions failed: {e}", exc_info=True)
            return None

    def _decode_and_emit(self, audio_paths, boundaries: Optional[List[Dict]],
                         num_targets: int) -> Tuple[Optional[Any], float]:
        return self._stream_emissions(audio_paths)

    def _emissions(self, waveform):
        """``[1, T, 29]`` log-probs for a ``[1, samples]`` 16 kHz waveform."""
        samples = waveform[0]
        return self._emissions_from_windows(
            samples[start:start + _WINDOW_SAMPLES] for start in range(0, samples.shape[0], _WINDOW_SAMPLES)
        )

    def _emissions_from_windows(self, windows) -> Any:
        """``[1, T, 29]`` log-probs for consecutive ``_WINDOW_SAMPLES`` windows."""
        import numpy as np

        parts = []
        for window in windows:
            window = np.asarray(window, dtype=np.float32)
            if window.shape[0] <= _MIN_WINDOW_SAMPLES:
                continue
            emphasised = np.empty_like(window)
            emphasised[0] = window[0]
            emphasised[1:] = window[1:] - _PRE_EMPHASIS * window[:-1]
            signal = np.pad(emphasised, _PAD_SAMPLES, mode="reflect")[None, None, :]
            log_probs = self._session.run(None, {"signal": signal})[0][0]
            log_probs[:, _BLANK_ID] = np.logaddexp(log_probs[:, _BLANK_ID], log_probs[:, _SPACE_ID])
            log_probs[:, _SPACE_ID] = _FOLDED_SPACE_SCORE
            if window.shape[0] == _WINDOW_SAMPLES:
                log_probs = log_probs[:_FRAMES_PER_WINDOW]
            parts.append(log_probs)
        return np.concatenate(parts)[None, :, :]

    def _alignment_backend(self) -> Tuple[Any, Any]:
        return None, None

    def _single_pass_fits(self, device, num_frames: int, num_targets: int) -> bool:
        return num_frames * (2 * num_targets + 1) <= self._MAX_SINGLE_PASS_CELLS

    def _segment_word_times(self, F, torch, emission, word_tokens: List[List[int]],
                            seconds_per_frame: float, frame_offset: int = 0):
        """Viterbi one emission slice against ``word_tokens``; per-word start times.

        The slice carries ``_CHUNK_MARGIN_SECONDS`` of neighbouring narration on each
        side; a skippable wildcard at both ends absorbs it, so the chunk's first and
        last words are not dragged into the margin.
        """
        targets = [tok for ids in word_tokens for tok in ids]
        if not targets:
            return None
        starts = ctc_viterbi_first_frames(emission[0], [_STAR_ID] + targets + [_STAR_ID], _BLANK_ID,
                                          star=_STAR_ID)
        if starts is None:
            return None
        starts = starts[1:-1]
        if min(starts) < 0:
            return None
        times: List[float] = []
        cursor = 0
        for ids in word_tokens:
            times.append(max(0.0, (frame_offset + starts[cursor]) * seconds_per_frame + _ONSET_OFFSET_SECONDS))
            cursor += len(ids)
        return times

    # Anchor-to-anchor segments (see `_chunked_word_times`): each is at least this many
    # tokens (tiny anchor gaps are merged) and is widened by this much audio per side.
    _MIN_SEGMENT_TOKENS = 200
    _SEGMENT_MARGIN_SECONDS = 2.0
    # One byte of back-pointer per frame x state: 256 MB for the largest segment.
    _MAX_SEGMENT_CELLS = 2**28

    def _chunked_word_times(self, F, torch, emission, kept: List[Tuple[str, int]],
                            word_tokens: List[List[int]], seconds_per_frame: float,
                            boundaries: List[Dict],
                            exclude_spans: Optional[List[Tuple[int, int]]] = None):
        """Align the words between consecutive ``boundaries`` anchors, one segment each.

        This is Storyteller's ``ctcForcedAlign`` layout: the chapter search's anchors
        come from these same emissions, so a segment's audio is known to within a
        frame and needs no wide margin. The narrow margin and the segment's edge
        wildcards absorb the error of a coarser prior (a transcript-derived map).
        MMS's single wide-margin chunks lose QuartzNet's edge words instead: on The
        Employees they dropped 913 of 21,519 words at the seams.
        """
        import bisect as _bisect

        total_frames = self._frames(emission)
        margin = max(1, int(round(self._SEGMENT_MARGIN_SECONDS / seconds_per_frame)))
        anchors: List[Tuple[int, float]] = []
        for b in sorted(boundaries, key=lambda b: b.get("char", b.get("global_char", 0))):
            char, ts = int(b.get("char", b.get("global_char", 0))), float(b["ts"])
            if not anchors or (char > anchors[-1][0] and ts >= anchors[-1][1]):
                anchors.append((char, ts))
        kept_chars = [char for _word, char in kept]
        edges = sorted({0, len(kept)} | {_bisect.bisect_left(kept_chars, char) for char, _ts in anchors})
        edges = [e for e in edges if 0 <= e <= len(kept)]

        def token_count(lo: int, hi: int) -> int:
            return sum(len(t) for t in word_tokens[lo:hi])

        word_ts: List[Optional[float]] = [None] * len(kept)
        failed_words = 0
        i = 0
        while i < len(kept):
            j = next((e for e in edges if e > i), len(kept))
            while j < len(kept) and token_count(i, j) < self._MIN_SEGMENT_TOKENS:
                j = next((e for e in edges if e > j), len(kept))
            char_lo = kept[i][1]
            char_hi = kept[j][1] if j < len(kept) else kept[-1][1] + 1
            f_lo = max(0, int(self._interp_ts(boundaries, char_lo) / seconds_per_frame) - margin)
            f_hi = min(total_frames, int(self._interp_ts(boundaries, char_hi) / seconds_per_frame) + margin)
            if j >= len(kept):
                f_hi = total_frames
            # A segment is never split inside: narration is not evenly spread between
            # two anchors (Lolita has 2,121 chars over 591 s, most of it non-text
            # audio), and every way of cutting it misplaced a half by minutes. One
            # too big for a single pass is left to interpolation between its anchors.
            tokens = token_count(i, j)
            times = None
            if f_hi - f_lo > tokens and \
                    (f_hi - f_lo) * (2 * tokens + 3) <= self._MAX_SEGMENT_CELLS:
                times = self._segment_word_times(F, torch, emission[:, f_lo:f_hi], word_tokens[i:j],
                                                 seconds_per_frame, frame_offset=f_lo)
            if times is None:
                failed_words += j - i
            else:
                word_ts[i:j] = times
            i = j

        if failed_words:
            logger.warning(
                "⚠️ CTC: %d words could not be aligned in their anchor segments; they are "
                "interpolated between the anchors", failed_words,
            )
        return [(kept[k][1], word_ts[k]) for k in range(len(kept)) if word_ts[k] is not None]

    def ctc_vocab(self) -> Tuple[int, Dict[int, str]]:
        self._load()
        return _BLANK_ID, {index: char for char, index in self._dict.items()}
