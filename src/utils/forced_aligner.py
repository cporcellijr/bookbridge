"""CTC forced alignment via torchaudio MMS_FA (issue #426, Phase B).

Aligns an audiobook directly against the *known* ebook text and emits dense
per-word ``{char, ts}`` anchors — no transcription and no lexical n-gram matching.
The output slots straight into the same alignment map the lexical pipeline
produces, in the canonical ``full_text`` character space that
``EbookParser.get_locator_from_char_offset`` re-parses.

torch/torchaudio are heavy and ship only in the opt-in ``-ctc`` image, so every
import here is lazy: on the standard image ``ForcedAligner.is_available()`` is
False and callers fall back to the Whisper/lexical pipeline. Any failure inside
``align`` returns ``None`` for the same reason — a broken alignment must never be
worse than the existing path.

MMS_FA facts (verified against torchaudio 2.11): the token vocabulary is lowercase
``a``–``z`` plus apostrophe (blank ``-`` = index 0, star ``*`` = last index), at a
16 kHz sample rate. Words are normalized to that character set; anything else
(digits, punctuation) is dropped, and a word left empty is skipped — its char
offset simply does not appear as an anchor, which the dense remaining anchors and
linear interpolation absorb.
"""

from __future__ import annotations

import bisect
import logging
import os
import re
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# MMS_FA accepts lowercase latin + apostrophe only.
_MMS_CHARS_RE = re.compile(r"[^a-z']")
# Emission is computed in windows of this many seconds to bound peak memory on the
# model forward pass for long audiobooks; the log-prob frames are concatenated.
_EMIT_WINDOW_SECONDS = 30


class ForcedAligner:
    """Lazily-loaded torchaudio MMS_FA forced aligner (see module docstring)."""

    def __init__(self):
        self._model = None
        self._dict: Optional[Dict[str, int]] = None
        self._device = None
        self._sample_rate = 16000
        # Temp decoded-audio files to unlink after each align (see _load_audio).
        self._tmp_audio_files: List[str] = []

    # -- availability -------------------------------------------------------- #

    @staticmethod
    def is_available() -> bool:
        """True when torch + torchaudio are importable (i.e. on the -ctc image)."""
        import importlib.util
        try:
            return bool(
                importlib.util.find_spec("torch")
                and importlib.util.find_spec("torchaudio")
            )
        except (ImportError, ValueError):
            return False

    def _resolve_device(self) -> str:
        pref = os.environ.get("CTC_DEVICE", "auto").strip().lower()
        import torch
        if pref == "cuda":
            return "cuda" if torch.cuda.is_available() else "cpu"
        if pref == "cpu":
            return "cpu"
        return "cuda" if torch.cuda.is_available() else "cpu"

    def _load(self):
        """Load and cache the MMS_FA model + token dictionary."""
        if self._model is not None:
            return
        import torch  # noqa: F401
        import torchaudio

        bundle = torchaudio.pipelines.MMS_FA
        self._sample_rate = bundle.sample_rate
        self._device = self._resolve_device()
        model = os.environ.get("CTC_MODEL", "mms_fa").strip().lower()
        if model not in ("", "mms_fa"):
            logger.warning(
                f"⚠️ CTC: unknown CTC_MODEL '{model}', using mms_fa (the only supported bundle)"
            )
        logger.info(f"⚙️ CTC: loading MMS_FA forced aligner on {self._device}")
        self._model = bundle.get_model(with_star=False).to(self._device).eval()
        self._dict = bundle.get_dict()

    # -- pure helpers (unit-tested without torch) ---------------------------- #

    @staticmethod
    def _book_words(full_text: str) -> List[Tuple[str, int]]:
        """Return ``[(mms_word, char_offset)]`` for each whitespace token.

        ``char_offset`` indexes into ``full_text`` (the canonical coordinate space
        the locator resolver re-parses). Words that hold no MMS-alignable character
        are dropped, so every entry can be tokenized and aligned.
        """
        words: List[Tuple[str, int]] = []
        for match in re.finditer(r"\S+", full_text):
            mms = _MMS_CHARS_RE.sub("", match.group().lower())
            if mms:
                words.append((mms, match.start()))
        return words

    @staticmethod
    def _build_map(
        entries: List[Tuple[str, int]],
        word_start_times: List[float],
        full_text: str,
    ) -> List[Dict]:
        """Assemble the ``[{char, ts}]`` map from aligned word start times.

        ``entries`` and ``word_start_times`` are parallel and already in reading
        order, so the anchors are monotonic by construction. A ``(0, 0.0)`` head
        and a ``(len(full_text), last_ts)`` tail are added so interpolation covers
        the whole book.
        """
        if not entries or len(entries) != len(word_start_times):
            return []
        anchors: List[Dict] = []
        if entries[0][1] > 0:
            anchors.append({"char": 0, "ts": 0.0})
        for (_word, char), ts in zip(entries, word_start_times):
            anchors.append({"char": int(char), "ts": round(float(ts), 3)})
        last_ts = anchors[-1]["ts"]
        if anchors[-1]["char"] < len(full_text):
            anchors.append({"char": len(full_text), "ts": last_ts})
        return anchors

    # -- alignment ----------------------------------------------------------- #

    def _emissions(self, waveform):
        """Model forward in windows; concatenate log-prob frames [1, T, C]."""
        import torch

        window = int(_EMIT_WINDOW_SECONDS * self._sample_rate)
        total = waveform.size(1)
        chunks = []
        with torch.inference_mode():
            for start in range(0, total, window):
                piece = waveform[:, start : start + window].to(self._device)
                emission, _ = self._model(piece)
                # forced_align dispatches on the emission device.
                chunks.append(emission)
                if len(chunks) % 10 == 0 or start + window >= total:
                    logger.info(
                        "⚙️ CTC: emissions %.0f/%.0fs on %s",
                        min(start + window, total) / self._sample_rate,
                        total / self._sample_rate, self._device,
                    )
        return torch.cat(chunks, dim=1)

    def _load_audio(self, audio_paths):
        """Decode one or more parts to a mono float32 waveform at the model rate.

        Decoding goes through the ffmpeg CLI (already required by the image for audio
        normalization) rather than ``torchaudio.load``: it accepts every audiobook
        container (m4b/mp3/…), downmixes and resamples in one pass, and avoids
        torchaudio's torchcodec backend and its strict FFmpeg-version coupling.

        All parts are streamed sequentially into a single temp file on disk and the
        returned waveform is a memory-map of it, so RAM stays bounded to the windows
        ``_emissions`` actually touches. Decoding a multi-hour book straight into a RAM
        buffer (subprocess PIPE + numpy copy) peaked at several GB and OOM-killed the
        container (#426). The temp file is unlinked by ``align`` after use.
        """
        import subprocess
        import tempfile

        import numpy as np
        import torch

        if isinstance(audio_paths, (str, os.PathLike)):
            audio_paths = [audio_paths]

        tmp = tempfile.NamedTemporaryFile(suffix=".f32le", delete=False)
        tmp_path = tmp.name
        tmp.close()
        try:
            with open(tmp_path, "wb") as out:
                for path in audio_paths:
                    subprocess.run(
                        [
                            "ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(path),
                            "-f", "f32le", "-ac", "1", "-ar", str(self._sample_rate), "pipe:1",
                        ],
                        stdout=out, stderr=subprocess.PIPE, check=True,
                    )
            num_samples = os.path.getsize(tmp_path) // 4
            if num_samples == 0:
                raise ValueError("ffmpeg produced no audio samples")
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        self._tmp_audio_files.append(tmp_path)
        mm = np.memmap(tmp_path, dtype="<f4", mode="r", shape=(num_samples,))
        return torch.from_numpy(mm).unsqueeze(0)

    def _cleanup_tmp_audio(self):
        while self._tmp_audio_files:
            path = self._tmp_audio_files.pop()
            try:
                os.unlink(path)
            except OSError:
                pass

    def _target_tokens(self, full_text: str, text_range: Optional[Tuple[int, int]] = None,
                       exclude_spans: Optional[List[Tuple[int, int]]] = None
                       ) -> Tuple[List[List[int]], List[Tuple[str, int]]]:
        """Build per-word CTC token ids for the words `align()` would actually target.

        Re-derives the same ``word_tokens``/``kept`` pair `align()` builds: words from
        `_book_words` within ``text_range``, with any word inside `exclude_spans`
        dropped, each converted to token ids via `self._dict`. This is the single
        implementation of that bookkeeping — `align()` and `can_single_pass` both call
        it, so a doomed pass's target-token count is always derived the same way it
        will actually be counted.

        Requires `self._dict` to already be loaded (call `self._load()` first).
        Returns ``([], [])`` when ``text_range`` is invalid or no word survives.
        """
        start, end = text_range if text_range is not None else (0, len(full_text))
        if not 0 <= start < end <= len(full_text):
            return [], []
        spans: List[Tuple[int, int]] = []
        for lo, hi in sorted(exclude_spans or []):
            lo, hi = max(start, lo), min(end, hi)
            if lo >= hi:
                continue
            if spans and lo <= spans[-1][1]:
                spans[-1] = (spans[-1][0], max(spans[-1][1], hi))
            else:
                spans.append((lo, hi))
        entries = [(word, char + start) for word, char in self._book_words(full_text[start:end])]
        word_tokens: List[List[int]] = []
        kept: List[Tuple[str, int]] = []
        span_idx = 0
        for word, char in entries:
            while span_idx < len(spans) and spans[span_idx][1] <= char:
                span_idx += 1
            if span_idx < len(spans) and spans[span_idx][0] <= char < spans[span_idx][1]:
                continue
            ids = [self._dict[c] for c in word if c in self._dict]
            if not ids:
                continue
            word_tokens.append(ids)
            kept.append((word, char))
        return word_tokens, kept

    def can_single_pass(self, audio_duration_seconds: float, full_text: str,
                        text_range: Optional[Tuple[int, int]] = None,
                        exclude_spans: Optional[List[Tuple[int, int]]] = None) -> bool:
        """Decode-free pre-flight: would a single forced_align pass fit this book?

        Mirrors the sizing check `align()` only discovers after paying for a full
        audio decode (issue #426: a 106,703s/846 MB book cost ~59s of pure waste
        decoding once just to learn it needed the chunked path, then decoded again
        for that path). Frames are estimated from ``audio_duration_seconds`` exactly
        as `align()` estimates them from a decoded waveform
        (``waveform.size(1) // 320`` — the MMS/wav2vec2 downsample factor already
        assumed there), so ``int(audio_duration_seconds * self._sample_rate) // 320``
        reproduces the same estimate without decoding anything. Loads the model
        (`self._load()`) since the sizing decision needs `self._dict` (to count
        target tokens via `_target_tokens`) and `self._device`, but never touches
        audio.

        Returns False when the aligner is unavailable, the model fails to load, or
        the text yields no alignable tokens in range — all of these already mean
        `align()` itself would fail, so it is always safe to skip a decode over them.
        """
        if not self.is_available():
            return False
        try:
            self._load()
            word_tokens, _kept = self._target_tokens(full_text, text_range, exclude_spans)
            if not word_tokens:
                return False
            num_targets = sum(len(ids) for ids in word_tokens)
            est_frames = max(1, int(audio_duration_seconds * self._sample_rate) // 320)
            return self._single_pass_fits(self._device, est_frames, num_targets)
        except Exception:
            logger.error("❌ CTC pre-flight sizing check failed", exc_info=True)
            return False

    def align(self, audio_paths: str | os.PathLike | List[str | os.PathLike], full_text: str,
              text_range: Optional[Tuple[int, int]] = None,
              boundaries: Optional[List[Dict]] = None,
              exclude_spans: Optional[List[Tuple[int, int]]] = None) -> Optional[List[Dict]]:
        """Force-align audio (one path or a list of parts) to ``full_text``.

        A single forced_align pass costs ~frames x tokens and only fits short books.
        When ``boundaries`` (an existing ``[{char, ts}]`` lexical map) is supplied and
        a single pass would be too large, the book is aligned in text-partitioned
        chunks whose audio windows come from those boundaries — so any length fits.
        Half-open ``exclude_spans`` contain unnarrated text: omit their words and
        break chunks at each gap, leaving the final map to interpolate across it.

        Returns a ``{char, ts}`` map, or ``None`` on any failure (missing deps,
        decode error, empty text) so the caller can fall back to the lexical pipeline.
        """
        if not self.is_available():
            return None
        start, end = text_range if text_range is not None else (0, len(full_text))
        if not 0 <= start < end <= len(full_text):
            return None
        spans: List[Tuple[int, int]] = []
        for lo, hi in sorted(exclude_spans or []):
            lo, hi = max(start, lo), min(end, hi)
            if lo >= hi:
                continue
            if spans and lo <= spans[-1][1]:
                spans[-1] = (spans[-1][0], max(spans[-1][1], hi))
            else:
                spans.append((lo, hi))
        if spans and (not boundaries or len(boundaries) < 2):
            logger.warning("⚠️ CTC: unnarrated spans require lexical boundaries; falling back to lexical")
            return None
        entries = [(word, char + start) for word, char in self._book_words(full_text[start:end])]
        if not entries:
            logger.warning("⚠️ CTC: no alignable words in ebook text; skipping")
            return None
        try:
            import torch
            import torchaudio.functional as F

            self._load()

            word_tokens, kept = self._target_tokens(full_text, text_range, exclude_spans)
            if not word_tokens:
                return None
            num_targets = sum(len(ids) for ids in word_tokens)

            logger.info("⚙️ CTC: decoding audio at %s Hz", self._sample_rate)
            waveform = self._load_audio(audio_paths)

            # Skip the expensive emissions pass when a single forced_align could not fit
            # anyway and there is no prior map to chunk against (a new long book on its
            # first attempt): estimate frames from the sample count (MMS/wav2vec2 downsample
            # ~320 samples/frame) so the doomed pass costs only a decode, not a GPU forward.
            est_frames = max(1, waveform.size(1) // 320)
            can_chunk = bool(boundaries and len(boundaries) >= 2)
            if not can_chunk and not self._single_pass_fits(self._device, est_frames, num_targets):
                if getattr(self._device, "type", self._device) == "cpu":
                    logger.warning(
                        "⚠️ CTC: CPU alignment exceeds the safe back-pointer limit "
                        "(~%s frames, %s tokens); falling back to lexical alignment",
                        est_frames, num_targets,
                    )
                else:
                    logger.warning(
                        "⚠️ CTC: alignment too large for a single GPU pass and no prior map "
                        "to chunk against (~%s frames, %s tokens); falling back to lexical",
                        est_frames, num_targets,
                    )
                return None

            logger.info(
                "⚙️ CTC: decoded %.0fs audio; computing emissions on %s",
                waveform.size(1) / self._sample_rate, self._device,
            )
            emission = self._emissions(waveform)  # [1, T, C], log-probs
            num_frames = emission.size(1)
            seconds_per_frame = waveform.size(1) / num_frames / self._sample_rate

            # forced_align allocates a work buffer that grows ~with frames x tokens.
            # An oversized single pass does not raise a catchable error — it aborts
            # the whole process (CPU: 32-bit back-pointer overflow; GPU: the CUDA
            # kernel exceeds device memory and aborts, taking the container down, #426).
            if not spans and self._single_pass_fits(emission.device, num_frames, num_targets):
                logger.info(
                    "⚙️ CTC: forced_align on %s (%s frames, %s tokens)",
                    emission.device, num_frames, num_targets,
                )
                word_times = self._segment_word_times(
                    F, torch, emission, word_tokens, seconds_per_frame,
                )
                if word_times is None:
                    return None
                char_times = [(kept[i][1], word_times[i]) for i in range(len(kept))]
            elif boundaries and len(boundaries) >= 2:
                logger.info(
                    "⚙️ CTC: chunked forced_align (%s frames, %s tokens, %s words) against "
                    "%s lexical boundary points",
                    num_frames, num_targets, len(kept), len(boundaries),
                )
                char_times = self._chunked_word_times(
                    F, torch, emission, kept, word_tokens, seconds_per_frame, boundaries,
                    exclude_spans=spans,
                )
            else:
                # Too large for one pass and no prior map to chunk against.
                if emission.device.type == "cpu":
                    logger.warning(
                        "⚠️ CTC: CPU alignment exceeds the safe back-pointer limit "
                        "(%s frames, %s tokens); falling back to lexical alignment",
                        num_frames, num_targets,
                    )
                else:
                    logger.warning(
                        "⚠️ CTC: alignment too large for a single GPU pass and no prior "
                        "map to chunk against (%s frames, %s tokens); falling back to lexical",
                        num_frames, num_targets,
                    )
                return None

            anchors = self._stitch_char_times(char_times)
            if len(anchors) < 2:
                logger.warning("⚠️ CTC: too few aligned anchors; falling back to lexical")
                return None
            # Keep canonical EPUB offsets, but do not map the end of narration to
            # the end of an unnarrated bonus excerpt.
            alignment_map = self._build_map(
                [(None, ch) for ch, _ts in anchors],
                [ts for _ch, ts in anchors],
                full_text[:end],
            )
            logger.info(
                f"🎯 CTC: forced-aligned {len(anchors)} words -> {len(alignment_map)} anchors "
                f"({num_frames} frames, {seconds_per_frame * num_frames:.0f}s audio)"
            )
            return alignment_map or None

        except Exception as e:
            logger.error(f"❌ CTC forced alignment failed: {e}", exc_info=True)
            return None
        finally:
            self._cleanup_tmp_audio()

    # -- single-pass sizing + chunked alignment ------------------------------ #

    # Text is partitioned into chunks of at most this many CTC tokens; each chunk's
    # forced_align then costs ~chunk_frames x chunk_tokens, tiny regardless of book
    # length. ~8000 chars of a normal narration is a few minutes of audio.
    _MAX_CHUNK_TOKENS = 8000
    # Extra audio kept on each side of a chunk so a slightly-off lexical boundary
    # still contains the chunk's true speech (edge audio is absorbed by forced_align).
    _CHUNK_MARGIN_SECONDS = 15.0
    # Bound total second-pass native work as well as each call's memory footprint.
    _MAX_RECOVERY_CELLS = 2**38

    def _single_pass_fits(self, device, num_frames: int, num_targets: int) -> bool:
        """Whether one forced_align over the whole book is safe on this device."""
        import torch
        cost = num_frames * (2 * num_targets + 1)
        if getattr(device, "type", device) == "cpu":
            return cost <= 2**31 - 1  # torchaudio's CPU back-pointer index is int32
        try:
            free_bytes, _total = torch.cuda.mem_get_info()
        except Exception:
            free_bytes = 0
        # ~0.6 B per frame*token observed on a 12 GB card (Buy a Bullet ~2e10 fit; a
        # 5.7 h book ~5e11 aborted); keep a safety margin.
        return bool(free_bytes and cost * 0.6 < free_bytes * 0.7)

    def _segment_word_times(self, F, torch, emission, word_tokens: List[List[int]],
                            seconds_per_frame: float, frame_offset: int = 0):
        """forced_align one emission (slice) against ``word_tokens``.

        Returns per-word start times (parallel to ``word_tokens``, offset by
        ``frame_offset`` frames), or None if the token/span counts disagree.
        """
        targets = [tok for ids in word_tokens for tok in ids]
        if not targets:
            return None
        targets_t = torch.tensor([targets], dtype=torch.int32, device=emission.device)
        aligned, scores = F.forced_align(emission, targets_t, blank=0)
        # merge_tokens is a Python loop; move the small per-frame tensors to CPU once.
        spans = F.merge_tokens(aligned[0].cpu(), scores[0].cpu(), blank=0)
        if len(spans) != len(targets):
            logger.warning("⚠️ CTC: token/span mismatch (%s vs %s)", len(spans), len(targets))
            return None
        times: List[float] = []
        cursor = 0
        for ids in word_tokens:
            times.append((frame_offset + spans[cursor].start) * seconds_per_frame)
            cursor += len(ids)
        return times

    @staticmethod
    def _interp_ts(boundaries: List[Dict], char: int) -> float:
        """Linear-interpolate a timestamp at ``char`` from a ``[{char, ts}]`` map."""
        chars = [b.get("char", b.get("global_char", 0)) for b in boundaries]
        ts = [b["ts"] for b in boundaries]
        i = bisect.bisect_right(chars, char) - 1
        i = max(0, min(i, len(boundaries) - 2))
        c0, c1, t0, t1 = chars[i], chars[i + 1], ts[i], ts[i + 1]
        if c1 == c0:
            return float(t0)
        return float(t0 + (t1 - t0) * (char - c0) / (c1 - c0))

    def _recover_skipped_spans(self, F, torch, emission, kept: List[Tuple[str, int]],
                                word_tokens: List[List[int]], seconds_per_frame: float,
                                boundaries: List[Dict], word_ts: List[Optional[float]],
                                total_frames: int, exclude_spans: List[Tuple[int, int]]
                                ) -> List[Optional[float]]:
        """Recover missing words once, within CTC anchors and hard exclusion edges."""
        kept_chars = [char for _word, char in kept]
        breaks = {bisect.bisect_left(kept_chars, lo): hi for lo, hi in exclude_spans}
        edges = [0] + sorted(index for index in breaks if 0 < index < len(kept)) + [len(kept)]
        frames = [0] + [int(self._interp_ts(boundaries, breaks[index]) / seconds_per_frame)
                        for index in edges[1:-1]] + [total_frames]
        margin = max(1, int(round(self._CHUNK_MARGIN_SECONDS / seconds_per_frame)))
        # Prefix sums keep splitting linear in the word count, even for long gaps.
        offsets = [0]
        for tokens in word_tokens:
            offsets.append(offsets[-1] + len(tokens))
        work_left = self._MAX_RECOVERY_CELLS

        for region_start, region_end, audio_lo, audio_hi in zip(
                edges, edges[1:], frames, frames[1:]):
            i = region_start
            while i < region_end:
                if word_ts[i] is not None:
                    i += 1
                    continue
                j = i + 1
                while j < region_end and word_ts[j] is None:
                    j += 1
                # Only an exclusion edge can substitute for a missing CTC anchor.
                # At the book's outer edges we still leave the run interpolated.
                if (i == 0 or j == len(kept)):
                    i = j
                    continue
                fa = audio_lo if i == region_start else word_ts[i - 1] / seconds_per_frame
                fb = audio_hi if j == region_end else word_ts[j] / seconds_per_frame
                total_tokens = offsets[j] - offsets[i]
                if fb - fa <= total_tokens:
                    logger.debug("CTC recovery: words[%s:%s] have too little bracketing "
                                 "audio (%s frames for %s tokens); leaving gap",
                                 i, j, int(fb - fa), total_tokens)
                    i = j
                    continue

                p = i
                last_ts = fa * seconds_per_frame
                while p < j:
                    q = p
                    while q < j and offsets[q + 1] - offsets[p] <= self._MAX_CHUNK_TOKENS:
                        q += 1
                    if q == p:  # An individual word over the token limit cannot be split.
                        p += 1
                        continue
                    while True:
                        c0, c1 = offsets[p] - offsets[i], offsets[q] - offsets[i]
                        wlo = max(audio_lo, int(fa + (fb - fa) * c0 / total_tokens) - margin)
                        whi = min(audio_hi, int(fa + (fb - fa) * c1 / total_tokens) + margin)
                        token_count = c1 - c0
                        fits = (whi - wlo > token_count and
                                self._single_pass_fits(emission.device, whi - wlo, token_count))
                        if fits or q == p + 1:
                            break
                        q = p + (q - p) // 2
                    if not fits:
                        p = q
                        continue
                    cost = (whi - wlo) * (2 * token_count + 1)
                    if cost > work_left:
                        logger.warning("CTC recovery: work budget exhausted; leaving remaining gaps")
                        return word_ts
                    work_left -= cost
                    try:
                        times = self._segment_word_times(
                            F, torch, emission[:, wlo:whi], word_tokens[p:q],
                            seconds_per_frame, frame_offset=wlo,
                        )
                    except RuntimeError:
                        logger.debug("CTC recovery: words[%s:%s] failed; leaving gap",
                                     p, q, exc_info=True)
                        times = None
                    if times is not None:
                        for k, ts in enumerate(times):
                            # Overlapping windows can select an earlier occurrence.
                            # Preserve both bracketing anchors and previous recovery.
                            if last_ts <= ts <= fb * seconds_per_frame:
                                word_ts[p + k] = ts
                                last_ts = ts
                    p = q
                i = j
        return word_ts

    def _chunked_word_times(self, F, torch, emission, kept: List[Tuple[str, int]],
                            word_tokens: List[List[int]], seconds_per_frame: float,
                            boundaries: List[Dict],
                            exclude_spans: Optional[List[Tuple[int, int]]] = None):
        """Align text-partitioned chunks whose audio windows come from ``boundaries``.

        Each chunk holds a disjoint slice of words (so every word gets exactly one
        time); the audio window per chunk overlaps its neighbours by a margin so the
        chunk's true speech is fully contained. Returns ``[(char, ts)]`` for the words
        that aligned (failed chunks are skipped and covered by interpolation).
        """
        total_frames = emission.size(1)
        margin = max(1, int(round(self._CHUNK_MARGIN_SECONDS / seconds_per_frame)))
        kept_chars = [char for _word, char in kept]
        breaks = {bisect.bisect_left(kept_chars, lo): hi for lo, hi in (exclude_spans or [])}
        break_indices = sorted(index for index in breaks if 0 < index < len(kept))
        break_frames = [int(self._interp_ts(boundaries, breaks[index]) / seconds_per_frame)
                        for index in break_indices]
        word_ts: List[Optional[float]] = [None] * len(kept)
        n = len(kept)
        i = 0
        skipped_chunks = skipped_words = failed_chunks = failed_words = 0
        while i < n:
            j, tok = i, 0
            while j < n and tok + len(word_tokens[j]) <= self._MAX_CHUNK_TOKENS:
                if j > i and j in breaks:
                    break
                tok += len(word_tokens[j])
                j += 1
            if j == i:
                j = i + 1
            last_chunk = j >= n
            char_lo = kept[i][1]
            char_hi = kept[n - 1][1] if last_chunk else kept[j][1]
            if j in breaks:
                char_hi = breaks[j]
            f_lo = max(0, int(self._interp_ts(boundaries, char_lo) / seconds_per_frame) - margin)
            tail_margin = margin * 2 if last_chunk else margin
            f_hi = min(total_frames,
                       int(self._interp_ts(boundaries, char_hi) / seconds_per_frame) + tail_margin)
            # There is no audio for an excluded span. Meet at the right matched
            # word's onset: the left anchor starts a whole n-gram earlier. Keeping
            # the ordinary 15s overlap here drags the left phrase into the right
            # narration and stitching then discards valid words at the seam.
            region = bisect.bisect_right(break_indices, i)
            if region:
                f_lo = max(f_lo, break_frames[region - 1])
            if region < len(break_frames):
                f_hi = min(f_hi, break_frames[region])
            seg_tokens = sum(len(word_tokens[k]) for k in range(i, j))
            if f_hi - f_lo <= seg_tokens:
                logger.debug("CTC: chunk words[%s:%s] has too few frames (%s) for %s "
                             "tokens; skipping", i, j, f_hi - f_lo, seg_tokens)
                skipped_chunks += 1
                skipped_words += j - i
                i = j
                continue
            times = self._segment_word_times(
                F, torch, emission[:, f_lo:f_hi], word_tokens[i:j], seconds_per_frame,
                frame_offset=f_lo,
            )
            if times is not None:
                for k, ts in enumerate(times):
                    word_ts[i + k] = ts
            else:
                logger.debug("CTC: chunk words[%s:%s] failed; leaving a gap", i, j)
                failed_chunks += 1
                failed_words += j - i
            i = j

        word_ts = self._recover_skipped_spans(
            F, torch, emission, kept, word_tokens, seconds_per_frame, boundaries,
            word_ts, total_frames, exclude_spans or [],
        )

        if skipped_chunks or failed_chunks:
            remaining = sum(ts is None for ts in word_ts)
            recovered = skipped_words + failed_words - remaining
            logger.log(
                logging.WARNING if remaining else logging.INFO,
                "⚠️ CTC: %d chunk(s)/%d words skipped for too few frames and %d chunk(s)/%d "
                "words failed to align in first pass; recovery aligned %d words; "
                "%d words remain interpolated (usually compressed lexical timing in the source map)",
                skipped_chunks, skipped_words, failed_chunks, failed_words, recovered, remaining,
            )

        results: List[Tuple[int, float]] = [
            (kept[k][1], word_ts[k]) for k in range(len(kept)) if word_ts[k] is not None
        ]
        return results

    @staticmethod
    def _stitch_char_times(char_times: List[Tuple[int, float]]) -> List[Tuple[int, float]]:
        """Order by char and drop timestamp inversions at chunk seams (monotonic)."""
        out: List[Tuple[int, float]] = []
        last_ts = -1.0
        for ch, ts in sorted(char_times, key=lambda ct: ct[0]):
            if ts >= last_ts:
                out.append((int(ch), float(ts)))
                last_ts = ts
        return out
