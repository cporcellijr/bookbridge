"""Measure CTC forced-alignment word-onset error against Whisper word timestamps.

Storyteller's experimental CTC work fixed frame-to-time math and shifted word starts
to compensate for CTC's sharp probability peaks (a token's peak sits slightly after the
word's true onset). BookBridge takes the raw first-frame of each token span with no such
shift. This script quantifies the resulting bias/jitter so we can decide whether it is
worth correcting for our use case (position sync), rather than tuning blind.

Method: for one or more audio excerpts, transcribe with Whisper (which yields per-word
start times — the very timing source our lexical pipeline already trusts) and force-align
the same audio against that transcript with the CTC aligner. Because both operate on the
identical word sequence, each word's Whisper start and CTC start are directly comparable.
The reported delta is ``ctc_start - whisper_start`` (positive => CTC places the word later).

Reference caveat: Whisper's own onsets are imperfect, so a bias both methods share is not
visible here. What IS measured is whether CTC introduces drift *beyond* the timings we
already ship, and its magnitude relative to the sync tolerance.

Run in the -ctc container with the usual TRANSCRIPTION_PROVIDER / WHISPER_MODEL /
WHISPER_CPP_URL environment variables. No live database is opened or modified.
"""

import argparse
import bisect
import json
import logging
import math
import os
import statistics
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from src.utils.forced_aligner import ForcedAligner
from src.utils.polisher import Polisher
from src.utils.transcriber import AudioTranscriber

logger = logging.getLogger(__name__)


def _whisper_reference(segments: List[Dict]) -> List[Tuple[str, float]]:
    """Flatten Whisper segments to ``[(word_text, start_seconds)]`` in reading order."""
    reference: List[Tuple[str, float]] = []
    for segment in segments:
        words = segment.get("words")
        if not isinstance(words, list):
            continue
        for word in words:
            try:
                text = str(word["word"]).strip()
                start = float(word["start"])
            except (KeyError, TypeError, ValueError):
                continue
            if text and math.isfinite(start):
                reference.append((text, start))
    return reference


def _transcript_and_spans(reference: List[Tuple[str, float]]) -> Tuple[str, List[Tuple[int, int, float]]]:
    """Join reference words into one text and record each word's ``(char_start, char_end,
    whisper_start)`` span, matching the single-space join the CTC tokenizer will see."""
    parts: List[str] = []
    spans: List[Tuple[int, int, float]] = []
    pos = 0
    for text, start in reference:
        if parts:
            pos += 1  # the joining space
        spans.append((pos, pos + len(text), start))
        parts.append(text)
        pos += len(text)
    return " ".join(parts), spans


def _ctc_word_starts(aligner: ForcedAligner, excerpt: Path, transcript: str) -> Optional[Dict[int, float]]:
    """Force-align ``excerpt`` to ``transcript`` and return ``{char_offset: ctc_start}``.

    Drives the aligner's single-pass internals directly (the excerpt is short enough to
    fit one pass), so it returns the raw per-word onset before map assembly. Returns None
    when the excerpt is too large for one pass (shorten --seconds) or alignment failed.
    """
    import torch
    import torchaudio.functional as F

    aligner._load()
    word_tokens: List[List[int]] = []
    kept_chars: List[int] = []
    for word, char in aligner._book_words(transcript):
        ids = [aligner._dict[c] for c in word if c in aligner._dict]
        if not ids:
            continue
        word_tokens.append(ids)
        kept_chars.append(char)
    if not word_tokens:
        return {}
    try:
        waveform = aligner._load_audio([str(excerpt)])
        emission = aligner._emissions(waveform)
        num_frames = emission.size(1)
        seconds_per_frame = waveform.size(1) / num_frames / aligner._sample_rate
        num_targets = sum(len(ids) for ids in word_tokens)
        if not aligner._single_pass_fits(emission.device, num_frames, num_targets):
            return None
        times = aligner._segment_word_times(F, torch, emission, word_tokens, seconds_per_frame)
    finally:
        aligner._cleanup_tmp_audio()
    if times is None:
        return {}
    return {kept_chars[i]: times[i] for i in range(len(kept_chars))}


def _collect_deltas(spans: List[Tuple[int, int, float]],
                    ctc_by_char: Dict[int, float]) -> List[Dict]:
    """Pair each Whisper word with the first CTC anchor inside its char span."""
    sorted_chars = sorted(ctc_by_char)
    paired: List[Dict] = []
    for char_start, char_end, whisper_start in spans:
        i = bisect.bisect_left(sorted_chars, char_start)
        if i < len(sorted_chars) and sorted_chars[i] < char_end:
            ctc_start = ctc_by_char[sorted_chars[i]]
            paired.append({
                "char": sorted_chars[i],
                "whisper_start": round(whisper_start, 3),
                "ctc_start": round(ctc_start, 3),
                "delta_ms": round((ctc_start - whisper_start) * 1000, 1),
            })
    return paired


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--start", type=float, default=600, help="first excerpt start (s)")
    parser.add_argument("--seconds", type=float, default=90, help="excerpt length (s)")
    parser.add_argument("--count", type=int, default=1, help="number of excerpts to sample")
    parser.add_argument("--spacing", type=float, default=1200, help="gap between excerpt starts (s)")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.start < 0 or args.seconds <= 0 or args.count < 1:
        parser.error("start >= 0, seconds > 0, count >= 1")
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    aligner = ForcedAligner()
    if not aligner.is_available():
        raise SystemExit("torch/torchaudio unavailable — run in the -ctc image")

    all_deltas: List[float] = []
    windows: List[Dict] = []
    total_chars = 0
    total_seconds = 0.0
    samples: List[Dict] = []
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="ctc-onset-") as temporary:
        root = Path(temporary)
        polisher = Polisher()
        for index in range(args.count):
            offset = args.start + index * args.spacing
            excerpt = root / f"excerpt_{index}.wav"
            probe = subprocess.run(
                ["ffmpeg", "-v", "error", "-ss", str(offset), "-i", str(args.audio),
                 "-t", str(args.seconds), "-ac", "1", "-ar", "16000", str(excerpt)],
            )
            if probe.returncode != 0 or not excerpt.exists() or excerpt.stat().st_size == 0:
                logger.info("window %s @ %.0fs: no audio (past end?), stopping", index, offset)
                break
            transcriber = AudioTranscriber(root, None, polisher)
            segments = transcriber.process_audio(
                f"onset-{index}", [{"local_path": str(excerpt), "ext": ".wav"}],
                expected_duration=args.seconds,
            )
            reference = _whisper_reference(segments)
            if not reference:
                logger.info("window %s @ %.0fs: transcript had no word timestamps, skipping", index, offset)
                continue
            transcript, spans = _transcript_and_spans(reference)
            ctc_by_char = _ctc_word_starts(aligner, excerpt, transcript)
            if ctc_by_char is None:
                logger.info("window %s @ %.0fs: excerpt too large for one CTC pass, skipping", index, offset)
                continue
            paired = _collect_deltas(spans, ctc_by_char)
            deltas = [p["delta_ms"] / 1000.0 for p in paired]
            all_deltas.extend(deltas)
            total_chars += len(transcript)
            total_seconds += args.seconds
            if len(samples) < 30:
                samples.extend(paired[:10])
            windows.append({
                "start_seconds": round(offset, 1),
                "whisper_words": len(reference),
                "matched_words": len(paired),
                "median_abs_ms": round(statistics.median(abs(d) * 1000 for d in deltas), 1) if deltas else None,
            })
            logger.info("window %s @ %.0fs: %s words, matched %s, median|Δ| %s ms",
                        index, offset, len(reference), len(paired),
                        windows[-1]["median_abs_ms"])

    if not all_deltas:
        raise SystemExit("no word pairs measured — check audio path / whisper word timestamps")

    abs_ms = sorted(abs(d) * 1000 for d in all_deltas)
    signed_ms = [d * 1000 for d in all_deltas]

    def _pct(values: List[float], q: float) -> float:
        if not values:
            return 0.0
        return round(values[min(len(values) - 1, int(q * len(values)))], 1)

    chars_per_second = round(total_chars / total_seconds, 1) if total_seconds else 0.0
    median_abs = statistics.median(abs_ms)
    report = {
        "audio": str(args.audio),
        "provider": os.getenv("TRANSCRIPTION_PROVIDER", "local"),
        "model": os.getenv("WHISPER_MODEL", "base"),
        "windows": windows,
        "word_pairs": len(all_deltas),
        "bias_ms_mean_signed": round(statistics.fmean(signed_ms), 1),
        "bias_ms_median_signed": round(statistics.median(signed_ms), 1),
        "abs_error_ms_p50": round(median_abs, 1),
        "abs_error_ms_p90": _pct(abs_ms, 0.90),
        "abs_error_ms_p95": _pct(abs_ms, 0.95),
        "abs_error_ms_max": round(abs_ms[-1], 1),
        "abs_error_ms_stdev": round(statistics.pstdev(signed_ms), 1),
        "narration_chars_per_second": chars_per_second,
        "median_abs_position_error_chars": round(median_abs / 1000 * chars_per_second, 2),
        "elapsed_seconds": round(time.perf_counter() - started, 1),
        "note": "delta = ctc_start - whisper_start (positive => CTC later). Reference is "
                "Whisper word timestamps, not human ground truth; a bias both methods share "
                "is not visible. Position error uses narration char density; compare against "
                "the sync threshold (~0.5% of book length).",
        "samples": samples,
    }
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    logger.info(
        "\n=== CTC onset vs Whisper ===\n"
        "word pairs           : %s\n"
        "signed bias (mean)   : %+.1f ms  (median %+.1f ms)\n"
        "abs error p50/p90/p95: %.1f / %.1f / %.1f ms  (max %.1f)\n"
        "stdev                : %.1f ms\n"
        "≈ position error p50 : %.2f chars  (at %.1f chars/s narration)\n"
        "report               : %s",
        report["word_pairs"], report["bias_ms_mean_signed"], report["bias_ms_median_signed"],
        report["abs_error_ms_p50"], report["abs_error_ms_p90"], report["abs_error_ms_p95"],
        report["abs_error_ms_max"], report["abs_error_ms_stdev"],
        report["median_abs_position_error_chars"], chars_per_second, args.output,
    )


if __name__ == "__main__":
    main()
