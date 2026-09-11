"""
Alignment Service.
Handles the core logic for aligning ebook text with audio transcriptions
and storing the results in the database.
"""

import bisect
import json
import logging
import math
import os
import re
import shutil
from pathlib import Path
from statistics import median
from typing import List, Dict, Optional, Tuple

from src.utils.time_utils import utcnow

from src.db.models import BookAlignment, BookAlignmentBackup
from src.services import map_quality
from src.services.segment_fit import Segment, fit_segments, select_anchors
from src.utils.config_loader import env_truthy
from src.utils.ebook_utils import LRUCache
from src.utils.polisher import Polisher
from src.utils.logging_utils import time_execution

logger = logging.getLogger(__name__)

_CTC_UNNARRATED_DENSITY_MULTIPLIER = 4.0
_CTC_UNNARRATED_MIN_SPAN_CHARS = 1500
_CTC_UNNARRATED_MIN_TIMING_RATIO = 0.25
# Slack over the audio-shortfall budget, for density-estimate error.
_CTC_UNNARRATED_BUDGET_MARGIN = 1.5
_LEXICAL_ANCHOR_WORDS = 12


# ---------------------------------------------------------------------------
# Segment-aware map lookups (issue #426 phase 1)
#
# `alignment_map_json` stays one flat list sorted by `char`, exactly as it has
# always been. When `segments_json` is present, that flat list is no longer
# assumed globally sorted by `ts` too -- narration order can differ from spine
# order, so only *within* one segment does `ts` still ascend with `char`. These
# module-level helpers are pure (no DB, no `self`) so the classmethod lookup
# they support stays independently testable; see
# `docs/PLAN_OUT_OF_ORDER_NARRATION.md`, "Why the LIS is not simply wrong".
# ---------------------------------------------------------------------------

def _segment_for_char(segments: List[Dict], char: int) -> Optional[Dict]:
    """The one segment whose half-open ``[char_start, char_end)`` contains
    ``char``, or ``None`` when it falls in a gap between segments (or before
    the first / after the last). Segments are char-disjoint by construction
    (`segment_fit.fit_segments`), so at most one can match."""
    for segment in segments:
        if segment['char_start'] <= char < segment['char_end']:
            return segment
    return None


def _segment_for_ts(segments: List[Dict], ts: float) -> Optional[Dict]:
    """The one segment whose half-open ``[ts_start, ts_end)`` contains
    ``ts``, or ``None`` when it falls in audio with no matching text (e.g.
    opening/closing credits). Segments are ts-disjoint by construction, so
    at most one can match."""
    for segment in segments:
        if segment['ts_start'] <= ts < segment['ts_end']:
            return segment
    return None


def _nearest_segment_edge_ts(char: int, segments: List[Dict]) -> float:
    """The timestamp of whichever segment edge is char-nearest to ``char``,
    without interpolating.

    Two callers, both from `get_time_for_text`: when ``char`` lands *inside*
    a segment but the map's own bracketing points came from two different
    segments (sparse data straddling the boundary), the nearest edge is one
    of that segment's own two edges. When ``char`` lands in a gap between
    segments, it is the edge of whichever neighbouring segment is closer.
    Interpolating across the boundary instead of clamping here is exactly
    what produced "22% of the text inside 23 seconds" (see the plan doc).
    """
    containing = _segment_for_char(segments, char)
    if containing is not None:
        distance_to_start = char - containing['char_start']
        distance_to_end = containing['char_end'] - char
        return containing['ts_start'] if distance_to_start <= distance_to_end else containing['ts_end']

    best_ts: Optional[float] = None
    best_distance: Optional[int] = None
    for segment in segments:
        for edge_char, edge_ts in ((segment['char_start'], segment['ts_start']),
                                    (segment['char_end'], segment['ts_end'])):
            distance = abs(char - edge_char)
            if best_distance is None or distance < best_distance:
                best_distance = distance
                best_ts = edge_ts
    return best_ts


def _nearest_segment_edge_char(ts: float, segments: List[Dict]) -> int:
    """The char of whichever segment edge is ts-nearest to ``ts`` -- the
    ts-domain mirror of `_nearest_segment_edge_ts`. Used by
    `AlignmentService._interpolate_char_for_time` when a timestamp falls in
    no segment's ``[ts_start, ts_end)`` (audio with no matching text)."""
    best_char: Optional[int] = None
    best_distance: Optional[float] = None
    for segment in segments:
        for edge_ts, edge_char in ((segment['ts_start'], segment['char_start']),
                                    (segment['ts_end'], segment['char_end'])):
            distance = abs(ts - edge_ts)
            if best_distance is None or distance < best_distance:
                best_distance = distance
                best_char = edge_char
    return best_char


def _segments_to_json(segments: List[Segment]) -> str:
    """Serialize `Segment`s for storage: only the four fields the lookups
    need (`char_start`, `char_end`, `ts_start`, `ts_end`). `inliers` and
    `residual` are fit diagnostics, not lookup data, and are dropped.

    `ts_start` is clamped to 0 here, at the persistence boundary, only
    (issue #426 phase 3). A segment's fitted line can extrapolate below zero
    at its own `char_start` -- real Four Past Midnight data: -175.1s for the
    first placed segment, meaning the audio opens with ~175s of credits that
    have no matching ebook text. That negative value is meaningful, but a
    negative audio timestamp has no business being persisted and read back
    by lookups that treat `ts` as a real position. The in-memory `Segment`
    this function receives is deliberately left unclamped:
    `segment_fit.select_anchors` (and, before that, `_fit_boundary`'s own
    residual/inlier accounting) derives its line from the segment's own two
    edges, `(char_start, ts_start)` to `(char_end, ts_end)` -- clamping
    `ts_start` there would change the slope of that line and skew which
    anchors are retained. Every caller reaches this function only after that
    work is already done, so clamping exclusively here is safe.
    """
    return json.dumps([
        {
            "char_start": segment.char_start,
            "char_end": segment.char_end,
            "ts_start": max(0.0, segment.ts_start),
            "ts_end": segment.ts_end,
        }
        for segment in segments
    ])


class AlignmentService:
    # Max chars of a window sent to the embedder (~1000 tokens, safely under
    # nomic-embed-text's 2048-token context).
    _EMBED_WINDOW_MAX_CHARS = 4000

    # CTC acceptance gate (issue #426). A CTC map is only as good as its densest
    # coverage: the largest run of text with no anchor is interpolated linearly, so
    # a big gap is a big positional error. A degenerate near-linear map has one gap
    # spanning the whole book. Reject a new map whose worst gap exceeds this fraction
    # of the covered text. Regression against whatever map it would replace is a
    # separate decision, made downstream by `_publish_map` — not here.
    _CTC_MAX_GAP_FRACTION = 0.25
    # A prior map built with one of these methods is a placeholder, not a real
    # alignment, so it is always safe to replace — `_publish_map` never runs its
    # regression veto against one.
    _CTC_REPLACEABLE_METHODS = frozenset({"linear", "storyteller_linear"})

    def __init__(self, database_service, polisher: Polisher, ollama_client=None):
        self.database_service = database_service
        self.polisher = polisher
        self.ollama_client = ollama_client
        # Parsed alignment maps for the actively-synced books; see _get_alignment.
        self._alignment_cache = LRUCache(capacity=self._env_int("ALIGNMENT_CACHE_SIZE", 3))
        # Ebook lengths keyed by abs_id. Tiny scalars, unlike the map blobs, so a
        # plain dict is fine; invalidated alongside the map in _save_alignment.
        self._total_chars_cache: Dict[str, Optional[int]] = {}
        # Segment placement index keyed by abs_id; see _get_segments. A plain
        # dict, not an LRUCache, because "no segments" (None) is itself a
        # frequent, valid, cacheable answer -- unlike _alignment_cache, where a
        # missing DB row is deliberately never cached, here a dict is required
        # to tell "not yet loaded" apart from "loaded, and there are none".
        self._segments_cache: Dict[str, Optional[List[Dict]]] = {}
        # Lazily-built CTC forced aligner (holds a heavy cached model). Only ever
        # instantiated on the -ctc image when CTC alignment is requested.
        self._forced_aligner = None

    def _ollama_ready(self) -> bool:
        client = self.ollama_client
        return bool(client and client.is_configured())

    @staticmethod
    def _env_true(key: str, default: str = "true") -> bool:
        if key.startswith("OLLAMA_"):
            from src.api.llm_settings import llm_setting_truthy
            return llm_setting_truthy(key, default)
        return os.environ.get(key, default).lower() == "true"

    @staticmethod
    def _env_float(key: str, default: float) -> float:
        if key.startswith("OLLAMA_"):
            from src.api.llm_settings import llm_setting_value
            raw = llm_setting_value(key, str(default))
        else:
            raw = os.environ.get(key, default)
        try:
            return float(raw)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _env_int(key: str, default: int) -> int:
        if key.startswith("OLLAMA_"):
            from src.api.llm_settings import llm_setting_value
            raw = llm_setting_value(key, str(default))
        else:
            raw = os.environ.get(key, default)
        try:
            return int(float(raw))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _point_char(point: Dict) -> int:
        if 'global_char' in point:
            return int(point['global_char'])
        return int(point.get('char', 0))

    @time_execution
    def align_and_store(self, abs_id: str, raw_segments: List[Dict], ebook_text: str,
                        spine_chapters: List[Dict] = None) -> bool:
        """
        Main entry point for "Unified Alignment".
        
        Steps:
        1. Validate Structure: Ensure we aren't trying to align mismatched content.
           (e.g., if spine_chapters provided, check roughly if segment count matches or text length matches).
        2. Normalize: Use Polisher to clean both raw transcript and ebook text.
        3. Anchor: Run N-Gram alignment to map characters to timestamps.
        4. Rebuild: Fix fragmented sentences in transcript using ebook text as a guide.
        5. Store: Save ONLY the mapping and essential metadata to DB.
        """
        logger.info(f"AlignmentService: Processing {abs_id} (Text: {len(ebook_text)} chars, Segments: {len(raw_segments)})")

        # 1. Validation (Spine Check)
        # Note: This is soft validation. If lengths assume vastly different sizes, warn.
        # Implementation of full spine verification requires mapping chapters to segments.
        # For now, we trust the inputs but log warnings.
        ebook_len = len(ebook_text)
        # Estimate audio text length
        audio_text_rough = " ".join([s['text'] for s in raw_segments])
        audio_len = len(audio_text_rough)
        
        ratio = audio_len / ebook_len if ebook_len > 0 else 0
        if ratio < 0.5 or ratio > 1.5:
             logger.warning(f"⚠️ Alignment Size Mismatch: Audio text is {ratio:.2%} of Ebook text size.")

        # 2. Normalize & Rebuild
        # Rejoin fragmented sentences, such as the "Mr. Smith" case.
        # We pass ebook_text to help (though rebuild_fragmented_sentences uses simple heuristics currently)
        rebuilt_segments = self.polisher.rebuild_fragmented_sentences(raw_segments, ebook_text)
        logger.info(f"   Rebuilt segments: {len(raw_segments)} -> {len(rebuilt_segments)}")

        # 2b. Content-match guard — refuse to persist a map when the audio and ebook are
        # clearly different content (wrong edition / abridged / translation / mis-match).
        if not self._verify_content_match(rebuilt_segments, ebook_text, abs_id=abs_id):
            return False

        # 3. Anchored Alignment
        alignment_map, align_method, map_segments = self._generate_alignment_map_with_method(
            rebuilt_segments, ebook_text, abs_id=abs_id, spine_chapters=spine_chapters)

        if not alignment_map:
            logger.error("   ❌ Failed to generate alignment map.")
            return False

        # 4. Store to Database
        self._publish_map(abs_id, alignment_map, align_method, total_chars=ebook_len,
                          segments=map_segments)
        # A vetoed write still leaves a valid, better incumbent map in place — only a
        # genuinely missing/invalid map is a caller-visible failure (sync_manager marks
        # the job failed_permanent and deletes the audio cache after retries exhaust).
        return True

    @staticmethod
    def ctc_enabled() -> bool:
        """Whether CTC forced alignment is switched on (read per call)."""
        return AlignmentService._env_true("CTC_ENABLED", "false")

    @staticmethod
    def segmented_maps_enabled() -> bool:
        """Whether per-chapter segment placement (issue #426 phase 2) replaces the
        global monotonic LIS filter for books whose narration order differs from
        their spine order. Read per call — never cache at import or in __init__.
        Uses `env_truthy` (not `_env_true`) so the settings-UI checkbox's "on"
        spelling is honored, not just "true"."""
        return env_truthy("ALIGNMENT_SEGMENTED_MAPS", "false")

    @time_execution
    def align_forced_and_store(self, abs_id: str, audio_path: str, ebook_text: str,
                               spine_chapters: Optional[List[Dict]] = None,
                               audio_duration: Optional[float] = None) -> bool:
        """Build an alignment map by CTC forced alignment (issue #426, method 'ctc').

        Aligns the audio directly against ``ebook_text`` — no transcript — and stores
        a dense ``{char, ts}`` map. Returns False on any failure (missing deps, decode
        error, empty/short map) so the caller falls back to the lexical pipeline.

        ``audio_duration`` (seconds), when supplied, enables a decode-free pre-flight
        (issue #426 phase 3): when there is no chunking prior, a doomed single-pass
        attempt is refused before the audio is ever decoded, rather than only after
        paying for a full decode inside `ForcedAligner.align`. Omit it (the default)
        to keep every existing caller's behaviour unchanged.
        """
        if not ebook_text:
            return False

        from src.utils.forced_aligner import ForcedAligner
        if not ForcedAligner.is_available():
            logger.warning(
                "⚠️ CTC alignment requested but torch/torchaudio are unavailable "
                "(use the -ctc image); falling back to the lexical pipeline"
            )
            return False

        # Segment-placement guard (issue #426): a book whose stored map already
        # has segments was narrated out of spine order and placed by segment
        # fitting, not the global monotonic chain. `_chunked_word_times` derives
        # each chunk's audio window from the incumbent map's char->ts anchors,
        # which is only valid when char and ts both ascend together -- exactly
        # what a reordered book's map does not do across its segment boundaries.
        # CTC has no segment awareness, so it would window itself against a
        # non-monotonic mapping and overwrite a good segmented map with a worse
        # one (Four Past Midnight: 0.9690 segmented -> 0.3230 CTC). Refuse here
        # and let the caller keep the segmented map -- per-segment CTC chunking
        # is real future work, not this guard.
        if self._get_segments(abs_id):
            logger.info(
                "⚙️ CTC: %s is narrated out of spine order (its stored map is "
                "segmented) -- CTC cannot chunk across segment boundaries, so "
                "the existing segmented map is kept and this CTC pass is refused",
                abs_id,
            )
            return False

        if self._forced_aligner is None:
            self._forced_aligner = ForcedAligner()

        text_range = self._ctc_text_range(abs_id, ebook_text, spine_chapters)
        # An existing lexical map lets the aligner chunk a long book (its char->ts
        # anchors bound each chunk's audio window). Only trust it when it was built
        # against this exact text length, so the char spaces line up.
        #
        # Never window a CTC pass against a previous CTC map. The windows would come
        # from the very map this run replaces, so any error in it re-derives the same
        # windows and reproduces itself, with no remap able to escape. Four Past
        # Midnight rode that loop with a bit-identical gap start char across a full
        # re-alignment (#426). Refusing here returns None before the emissions pass
        # on a long book, so the caller rebuilds a transcript-derived map first and
        # CTC re-runs against that.
        boundaries = None
        prior = self._get_alignment(abs_id)
        prior_method = self.database_service.get_alignment_method(abs_id) or ""
        if prior and len(prior) >= 2 and prior_method != "ctc":
            total = self._get_alignment_total_chars(abs_id)
            if total is not None and total == len(ebook_text):
                boundaries = prior
        elif prior_method == "ctc":
            logger.info(
                "⚙️ CTC: ignoring the existing CTC map for %s as a chunking source — a CTC "
                "pass windowed against its own prior map cannot correct that map's errors",
                abs_id,
            )
        exclude_spans = self._detect_unnarrated_spans(boundaries, ebook_text)
        if text_range:
            exclude_spans = [(max(lo, text_range[0]), min(hi, text_range[1]))
                             for lo, hi in exclude_spans
                             if lo < text_range[1] and hi > text_range[0]]
        if exclude_spans:
            logger.info("⚙️ CTC: excluding likely unnarrated interior text for %s: %s",
                        abs_id, exclude_spans)

        # Decode-free pre-flight (issue #426 phase 3): when there is no chunking
        # prior, `align()` would decode the whole file only to then discover the
        # pass is too large and bail — audio duration and token count are both known
        # without decoding, so the doomed run is refused here instead. `boundaries`
        # is the same chunking prior `align()` itself would consult below.
        if (boundaries is None and audio_duration and audio_duration > 0
                and not self._forced_aligner.can_single_pass(
                    audio_duration, ebook_text, text_range=text_range, exclude_spans=exclude_spans)):
            logger.info(
                "⚙️ CTC: skipping the decode for %s — no chunking prior and the book is "
                "too large for a single pass",
                abs_id,
            )
            return False

        alignment_map = self._forced_aligner.align(
            audio_path, ebook_text, text_range=text_range, boundaries=boundaries,
            exclude_spans=exclude_spans,
        )
        if not alignment_map or len(alignment_map) < 2:
            logger.warning(f"⚠️ CTC alignment produced no usable map for {abs_id}")
            return False

        if not self._ctc_map_accepted(abs_id, alignment_map, exclude_spans):
            return False

        if not self._publish_map(abs_id, alignment_map, "ctc", total_chars=len(ebook_text),
                                 exclude_spans=exclude_spans):
            return False
        logger.info(
            f"AlignmentService: CTC forced-alignment map stored for {abs_id} "
            f"({len(alignment_map)} anchors)"
        )
        return True

    def _ctc_text_range(self, abs_id: str, ebook_text: str,
                        spine_chapters: Optional[List[Dict]]) -> Optional[Tuple[int, int]]:
        """Reuse a well-covered lexical map to exclude unnarrated outer chapters."""
        if not spine_chapters:
            return None
        previous = self._get_alignment(abs_id)
        if not previous:
            return None
        total_chars = self._get_alignment_total_chars(abs_id) or self._point_char(previous[-1])
        if total_chars != len(ebook_text):
            return None
        matched = self._ctc_matched_anchors(previous)
        if not matched:
            return None
        first_char, last_char = self._point_char(matched[0]), self._point_char(matched[-1])
        chapters = [chapter for chapter in spine_chapters
                    if chapter['end'] > first_char and chapter['start'] <= last_char]
        if not chapters:
            return None
        start, end = chapters[0]['start'], chapters[-1]['end']
        if not 0 <= start < end <= len(ebook_text):
            return None
        logger.info(
            "⚙️ CTC: using narrated EPUB chapters from existing lexical matches "
            "(chars %s:%s of %s)", start, end, len(ebook_text),
        )
        return start, end

    @classmethod
    def _ctc_matched_anchors(cls, prior_map: Optional[List[Dict]]) -> List[Dict]:
        """Return ordered transcript matches only when they cover 90% of the audio."""
        if not prior_map:
            return []
        matched = sorted((p for p in prior_map if 't_idx' in p), key=cls._point_char)
        duration = max(float(p['ts']) for p in prior_map)
        if len(matched) < 2 or not math.isfinite(duration) or duration <= 0:
            return []
        times = [float(p['ts']) for p in matched]
        if (any(not math.isfinite(t) or t < 0 for t in times)
                or any(b < a for a, b in zip(times, times[1:]))
                or times[-1] - times[0] < 0.9 * duration):
            return []
        return matched

    @classmethod
    def _detect_unnarrated_spans(cls, prior_map: Optional[List[Dict]],
                                ebook_text: str) -> List[Tuple[int, int]]:
        """Find large interior text jumps over little audio, preserving matched words."""
        matched = cls._ctc_matched_anchors(prior_map)
        pairs = [(cls._point_char(a), cls._point_char(b), float(b['ts']) - float(a['ts']),
                  int(b['t_idx']) - int(a['t_idx']))
                 for a, b in zip(matched, matched[1:])]
        densities = [(hi - lo) / seconds for lo, hi, seconds, _words in pairs
                     if 0 <= lo < hi < len(ebook_text) and seconds > 0]
        word_durations = [seconds / words for _lo, _hi, seconds, words in pairs
                          if seconds > 0 and words > 0]
        if not densities or not word_durations:
            return []
        median_density = median(densities)
        threshold = _CTC_UNNARRATED_DENSITY_MULTIPLIER * median_density
        min_word_duration = _CTC_UNNARRATED_MIN_TIMING_RATIO * median(word_durations)
        polisher = Polisher()
        spans: List[Tuple[int, int]] = []
        for lo, hi, seconds, words in pairs:
            if not 0 <= lo < hi < len(ebook_text):
                continue
            # Density (text with far too little audio) is the signal; a normal
            # per-word timing distinguishes a true gap from compressed-transcript
            # windows that merely squeeze real narration into a tiny jump. Anchors
            # are 12-gram starts, so the pair's Δts always spans ~12 narrated words
            # (several seconds) — an absolute seconds cap would suppress real gaps.
            if (hi - lo < _CTC_UNNARRATED_MIN_SPAN_CHARS
                    or words <= 0 or seconds <= 0 or seconds / words < min_word_duration
                    or (hi - lo) / seconds < threshold):
                continue
            # Anchors mark the START of a matching n-gram, not its last word.
            # Preserve the whole phrase (conservatively also for N=6 backfill).
            # Never merge across another match, even when flagged gaps are close.
            count = 0
            for word in re.finditer(r'\S+', ebook_text[lo:hi]):
                if polisher.normalize(word.group()):
                    count += 1
                if count == _LEXICAL_ANCHOR_WORDS:
                    lo += word.end()
                    break
            else:
                continue
            if hi - lo >= _CTC_UNNARRATED_MIN_SPAN_CHARS:
                spans.append((lo, hi))
        if not spans:
            return []
        # A gap is only real when the audio is too short to have narrated the text.
        # Local lexical-timing errors can spike density even on a fully-narrated book,
        # so cap total exclusions by the book's audio shortfall: if the flagged text
        # exceeds what the missing audio could account for, the map is unreliable for
        # exclusion and nothing is dropped.
        audio_seconds = max((float(p['ts']) for p in prior_map), default=0.0)
        budget = (len(ebook_text) - audio_seconds * median_density) * _CTC_UNNARRATED_BUDGET_MARGIN
        flagged = sum(hi - lo for lo, hi in spans)
        if flagged > budget:
            logger.info(
                "⚙️ CTC: %d flagged unnarrated chars exceed the %d-char audio-shortfall "
                "budget; lexical map unreliable for exclusion, dropping none",
                flagged, max(0, int(budget)),
            )
            return []
        return spans

    def _ctc_map_accepted(self, abs_id: str, new_map: List[Dict],
                          exclude_spans: Optional[List[Tuple[int, int]]] = None) -> bool:
        """Whether a freshly built CTC map is good enough to store (issue #426).

        A CTC map is only as good as its densest coverage: the largest run of text
        with no anchor is interpolated linearly, so a big gap is a big positional
        error. This rejects a degenerate/near-linear map (one gap spanning the whole
        book). Regression against whatever map it would replace is a separate
        decision, made by `_publish_map` once this absolute gate has cleared.
        """
        new_gap = self._max_gap_fraction(new_map, exclude_spans)
        if new_gap > self._CTC_MAX_GAP_FRACTION:
            logger.warning(
                "🚫 CTC: rejecting map for %s — largest interpolated gap is %.0f%% of the "
                "narrated text (> %.0f%%); keeping the existing map",
                abs_id, new_gap * 100, self._CTC_MAX_GAP_FRACTION * 100,
            )
            return False
        return True

    @staticmethod
    def _max_gap_fraction(alignment_map: List[Dict],
                          exclude_spans: Optional[List[Tuple[int, int]]] = None) -> float:
        """Largest char span between consecutive anchors, as a fraction of the map's
        covered range, with intentional exclusions removed from both. Returns
        1.0 for a map too small to judge (degenerate)."""
        return map_quality.max_gap_fraction(alignment_map, exclude_spans)

    @time_execution
    def align_storyteller_and_store(self, abs_id: str, storyteller_transcript, ebook_text: str = None) -> bool:
        """
        Build a chapter-aware alignment map directly from Storyteller wordTimeline data,
        anchored to the actual EPUB text to prevent global offset drifts.
        """
        if ebook_text:
            logger.info(f"AlignmentService: Anchoring Storyteller transcript for {abs_id} to {len(ebook_text)} chars of text...")
            
            segments = []
            
            # iter_alignment_points yields only timestamps/offsets; build text segments from chapter transcripts.
            for chapter_index, meta in enumerate(storyteller_transcript.chapters):
                try:
                    chapter = storyteller_transcript._load_chapter(chapter_index)
                    chapter_start = float(meta.get("start", 0.0) or 0.0)
                    transcript_text = chapter.get("transcript", "")
                    timeline = chapter.get("word_timeline", [])
                    
                    if not timeline or not transcript_text: continue
                    
                    # Keep small text windows for fallback matching, carrying
                    # the original word timings through EPUB re-anchoring.
                    seg_start = chapter_start + float(timeline[0].get("startTime", 0.0))
                    seg_text_words = []
                    seg_words = []
                    
                    for i, w in enumerate(timeline):
                        ts = float(w.get("startTime", 0.0)) + chapter_start
                        
                        # Extract word text; fall back to offset-based slicing when absent
                        word_text = w.get("word")
                        if not word_text:
                            # Use offset mapping
                            py_start = chapter["start_offsets_py"][i]
                            py_end = chapter["start_offsets_py"][i+1] if i+1 < len(timeline) else len(transcript_text)
                            word_text = transcript_text[py_start:py_end]
                            
                        seg_text_words.append(word_text.strip())
                        seg_words.append({
                            "word": word_text.strip(),
                            "start": ts,
                            "end": chapter_start + float(w.get("endTime", ts - chapter_start + 0.5)),
                        })
                        
                        # Break segment every ~15 seconds or on last word
                        if ts - seg_start > 15.0 or i == len(timeline) - 1:
                            segments.append({
                                "start": seg_start,
                                "end": max(ts, seg_words[-1]["end"]),
                                "text": " ".join(seg_text_words),
                                "words": seg_words,
                            })
                            if i + 1 < len(timeline):
                                seg_start = chapter_start + float(timeline[i + 1].get("startTime", 0.0))
                            seg_text_words = []
                            seg_words = []
                except Exception as e:
                    logger.warning(f"Error reading Storyteller chapter {chapter_index}: {e}", exc_info=True)
                    
            if segments:
                rebuilt_segments = self.polisher.rebuild_fragmented_sentences(segments, ebook_text)
                alignment_map, align_method, _map_segments = self._generate_alignment_map_with_method(
                    rebuilt_segments, ebook_text, abs_id=abs_id)
                if alignment_map:
                    if self._publish_map(abs_id, alignment_map, align_method, total_chars=len(ebook_text)):
                        logger.info(f"AlignmentService: Anchored Storyteller map stored for {abs_id} ({len(alignment_map)} points)")
                    # A vetoed write still leaves a valid, better incumbent map in place —
                    # only a missing map is a caller-visible failure (sync_manager marks
                    # the job failed_permanent and deletes the audio cache after retries
                    # exhaust).
                    return True
            
            logger.warning(f"AlignmentService: Anchored alignment failed for {abs_id}, falling back to unanchored map")

        # Fallback to unanchored map
        if ebook_text:
            clean_map = [
                {"char": 0, "ts": 0.0},
                {"char": len(ebook_text), "ts": storyteller_transcript.get_global_duration()},
            ]
            if self._publish_map(abs_id, clean_map, "storyteller_linear", total_chars=len(ebook_text)):
                logger.info(f"AlignmentService: Linear fallback map stored for {abs_id} ({len(clean_map)} points)")
            # Same rationale as the anchored-path veto above: a two-point linear
            # fallback vetoed against a better incumbent is not a failure.
            return True

        alignment_map = list(storyteller_transcript.iter_alignment_points())
        if not alignment_map:
            logger.error("   Failed to generate storyteller alignment map.")
            return False

        # Remap 'global_char' from iter_alignment_points to the 'char' key expected by _save_alignment.
        clean_map = []
        for pt in alignment_map:
            clean_map.append({
                "char": pt.get("global_char", 0),  # cumulative Python-index char offset
                "ts": pt.get("ts", 0.0)
            })

        if self._publish_map(abs_id, clean_map, "storyteller"):
            logger.info(f"AlignmentService: Unanchored Storyteller map stored for {abs_id} ({len(clean_map)} points)")
        # Same rationale: a veto here still leaves the existing, better map in place.
        return True

    def get_time_for_text(self, abs_id: str, query_text: str, char_offset_hint: int = None) -> Optional[float]:
        """
        Precise time lookup.
        If char_offset_hint is provided (from ebook reader), use it directly with the map.
        Otherwise, fuzzy search the text to find offset, then use map.
        """
        if char_offset_hint is None:
            # Note: For now, KOSync always provides an offset or we calculate it.
            return None
        return self.get_time_for_char(abs_id, char_offset_hint)

    def get_time_for_char(self, abs_id: str, char_offset: int) -> Optional[float]:
        """Interpolate the audio timestamp for a character offset in `abs_id`'s
        stored alignment map.

        Segment-aware (issue #426): the underlying map may be a segmented map
        (out-of-order narration), so this never interpolates across a segment
        boundary — it clamps to the nearest segment edge instead when the
        bracketing points belong to two different segments (or to none).
        """
        # 1. Fetch Alignment Map
        alignment = self._get_alignment(abs_id)
        if not alignment:
            return None

        map_points = alignment
        segments = self._get_segments(abs_id)
        target_offset = char_offset

        # 2. Interpolate Timestamp
        # Binary search
        left = 0
        right = len(map_points) - 1

        # Points are [{'char': x, 'ts': y}, ...]
        # Find interval [p1, p2] where p1.char <= target <= p2.char

        first_char = self._point_char(map_points[0])
        last_char = self._point_char(map_points[-1])

        if target_offset < first_char:
            return map_points[0]['ts']
        if target_offset > last_char:
            return map_points[-1]['ts']

        # Manual binary search to find floor
        floor_idx = 0
        while left <= right:
            mid = (left + right) // 2
            if self._point_char(map_points[mid]) <= target_offset:
                floor_idx = mid
                left = mid + 1
            else:
                right = mid - 1

        p1 = map_points[floor_idx]

        # Ceiling is next point
        if floor_idx + 1 < len(map_points):
            p2 = map_points[floor_idx + 1]
        else:
            return p1['ts']

        # Segment-aware clamp (issue #426 phase 1): never interpolate across a
        # segment boundary. If the bracketing points came from two different
        # segments (sparse data straddling one), or neither belongs to any
        # segment (the target is in a gap), clamp to the nearest segment edge
        # instead of blending two different sections of the audio timeline.
        if segments:
            p1_segment = _segment_for_char(segments, self._point_char(p1))
            p2_segment = _segment_for_char(segments, self._point_char(p2))
            if p1_segment is None or p1_segment is not p2_segment:
                return _nearest_segment_edge_ts(target_offset, segments)

        # Linear Interpolation
        p1_char = self._point_char(p1)
        p2_char = self._point_char(p2)
        char_span = p2_char - p1_char
        time_span = p2['ts'] - p1['ts']

        if char_span == 0: return p1['ts']

        ratio = (target_offset - p1_char) / char_span
        estimated_time = p1['ts'] + (time_span * ratio)

        return float(estimated_time)

    def get_char_for_time(self, abs_id: str, timestamp: float) -> Optional[int]:
        """
        Reverse lookup: Find character offset for a given timestamp.
        """
        alignment = self._get_alignment(abs_id)
        if not alignment:
            return None
        segments = self._get_segments(abs_id)
        return self._interpolate_char_for_time(alignment, timestamp, segments)

    @classmethod
    def _interpolate_char_for_time(cls, map_points: List[Dict], timestamp: float,
                                   segments: Optional[List[Dict]] = None) -> Optional[int]:
        """Interpolate the character offset for a timestamp within a loaded map.

        `segments` is the per-book segment index (issue #426 phase 1). When
        supplied and non-empty, the flat list is no longer assumed globally
        ts-sorted -- only within one segment does `ts` ascend with `char` --
        so `timestamp` is first placed into the one segment whose
        `[ts_start, ts_end)` contains it (segments are ts-disjoint by
        construction), and the search is restricted to that segment's own
        char slice of `map_points`, where the flat binary search is valid
        again. A timestamp inside no segment (audio with no matching text,
        e.g. credits) resolves to the nearest segment edge instead of
        searching the (for this purpose, wrongly ordered) full list.

        `segments=None` or `[]` reproduces today's single flat-list binary
        search unchanged -- the compatibility guarantee for every map stored
        before this shipped (`segments_json IS NULL`).
        """
        if not map_points:
            return None
        if not segments:
            return cls._interpolate_within(map_points, timestamp)

        segment = _segment_for_ts(segments, timestamp)
        if segment is None:
            return _nearest_segment_edge_char(timestamp, segments)

        segment_points = [point for point in map_points
                         if segment['char_start'] <= cls._point_char(point) < segment['char_end']]
        if not segment_points:
            # A placed segment with no matching flat-map points isn't expected
            # from any real producer, but degrade to its own edge rather than
            # fall through to a full-list search that assumes an ordering this
            # map no longer has.
            return _nearest_segment_edge_char(timestamp, [segment])
        return cls._interpolate_within(segment_points, timestamp)

    @classmethod
    def _interpolate_within(cls, map_points: List[Dict], timestamp: float) -> Optional[int]:
        """Binary-search `map_points` by `ts` and linearly interpolate `char`.

        Assumes `ts` ascends across `map_points` -- true for the whole flat
        map when no segments exist, and true within one segment's own char
        slice when they do (see `_interpolate_char_for_time`). This is the
        exact body of `_interpolate_char_for_time` from before segment
        awareness (issue #426 phase 1), factored out so both callers share
        one implementation instead of drifting apart.
        """
        target_ts = timestamp

        # Binary search for interval
        left = 0
        right = len(map_points) - 1

        if target_ts <= map_points[0]['ts']:
            return cls._point_char(map_points[0])
        if target_ts >= map_points[-1]['ts']:
            return cls._point_char(map_points[-1])

        floor_idx = 0
        while left <= right:
            mid = (left + right) // 2
            if map_points[mid]['ts'] <= target_ts:
                floor_idx = mid
                left = mid + 1
            else:
                right = mid - 1

        p1 = map_points[floor_idx]
        if floor_idx + 1 < len(map_points):
            p2 = map_points[floor_idx + 1]
        else:
            return cls._point_char(p1)

        # Interpolate
        time_span = p2['ts'] - p1['ts']
        p1_char = cls._point_char(p1)
        p2_char = cls._point_char(p2)
        char_span = p2_char - p1_char

        if time_span == 0:
            return p1_char

        ratio = (target_ts - p1['ts']) / time_span
        estimated_char = p1_char + (char_span * ratio)

        return int(estimated_char)

    def get_progress_for_time(self, abs_id: str, timestamp: float) -> Optional[float]:
        """
        Convert an audio timestamp into an ebook text-progress fraction (0..1)
        via the stored alignment map.

        Audio clients (ABS, etc.) report progress on the time axis
        (elapsed seconds / duration) while ebook clients report it on the text
        axis (characters / total characters). The two are not linearly related,
        so they can only be compared after mapping one onto the other. Returns
        None when no alignment exists for the book.
        """
        alignment = self._get_alignment(abs_id)
        if not alignment:
            return None

        # The map's last anchor is the last place the transcript matched the book,
        # not the end of the book. Dividing by it over-reports every position — by
        # a little on a healthy map, and by 1/coverage on a map that only spans
        # part of the text. Prefer the recorded ebook length; fall back to the old
        # denominator for maps stored before it was captured.
        max_char = self._point_char(alignment[-1])
        total_chars = self._get_alignment_total_chars(abs_id)
        # Anchors are offsets into the same text, so total_chars can never be the
        # smaller of the two. If it is, the record disagrees with the map it
        # belongs to — fall back rather than trust it.
        if not total_chars or total_chars < max_char:
            total_chars = max_char
        if total_chars <= 0:
            return None

        segments = self._get_segments(abs_id)
        char = self._interpolate_char_for_time(alignment, timestamp, segments)
        if char is None:
            return None

        return max(0.0, min(char / total_chars, 1.0))

    @staticmethod
    def _filter_monotonic_lis(anchors: List[Dict]) -> List[Dict]:
        """
        Return the longest subsequence of anchors (already sorted by 'char')
        with strictly increasing 'ts' values. O(n log n) patience sort.
        """
        n = len(anchors)
        if n <= 1:
            return list(anchors)

        tails: List[float] = []
        tail_idx: List[int] = []
        parent: List[int] = [-1] * n

        for i, anchor in enumerate(anchors):
            ts = anchor['ts']
            pos = bisect.bisect_left(tails, ts)
            if pos == len(tails):
                tails.append(ts)
                tail_idx.append(i)
            else:
                tails[pos] = ts
                tail_idx[pos] = i
            if pos > 0:
                parent[i] = tail_idx[pos - 1]

        result_indices: List[int] = []
        idx = tail_idx[-1]
        while idx != -1:
            result_indices.append(idx)
            idx = parent[idx]
        result_indices.reverse()
        return [anchors[i] for i in result_indices]

    def _generate_alignment_map(self, segments: List[Dict], full_text: str) -> List[Dict]:
        """Thin wrapper preserving the list-returning contract for callers/tests."""
        alignment_map, _method, _map_segments = self._generate_alignment_map_with_method(segments, full_text)
        return alignment_map

    def _timed_segment_tokens(self, segment: Dict) -> List[Dict]:
        """Use word timing only when it covers the segment text in order."""
        words = segment.get('words')
        if not isinstance(words, list) or not words:
            return []
        tokens = []
        previous_start = float(segment['start'])
        try:
            for word in words:
                start, end = float(word['start']), float(word['end'])
                if (not math.isfinite(start) or not math.isfinite(end)
                        or start < previous_start or end < start
                        or end > float(segment['end'])):
                    return []
                previous_start = start
                raw_words = word['word'].split()
                for raw in raw_words:
                    norm = self.polisher.normalize(raw)
                    if norm:
                        tokens.append({'word': norm, 'ts': start})
        except (KeyError, TypeError, ValueError, AttributeError):
            return []
        expected = [self.polisher.normalize(w) for w in segment['text'].split()]
        if [t['word'] for t in tokens] != [w for w in expected if w]:
            return []
        return tokens

    def _generate_alignment_map_with_method(
            self, segments: List[Dict], full_text: str, abs_id: Optional[str] = None,
            spine_chapters: Optional[List[Dict]] = None,
    ) -> Tuple[List[Dict], str, Optional[List[Segment]]]:
        """
        Core Anchored Alignment Algorithm (Two-Pass), returning (map, method, map_segments).
        Pass 1: High confidence (N=12) global search.
        Pass 2: Backfill start gap (N=6) if first anchor is late.
        method: 'lexical' (n-gram anchors), 'llm_anchor' (embedding rescue), or 'linear'.
        abs_id: optional book identifier, used only for the out-of-order-block
        diagnostic warning (issue #426); omitted from all other behavior.
        spine_chapters: optional EPUB spine chapter dicts (``{'start', 'end'}`` char
        offsets), used only to build boundaries for per-chapter segment placement
        (issue #426 phase 2, gated by `segmented_maps_enabled()`). ``None`` or empty
        keeps today's single global-LIS behavior. `map_segments` (the third return
        value) is ``None`` unless segment placement actually ran AND found the
        narration out of spine order; a caller persists it via `_save_alignment`'s
        `segments` parameter, and must not persist a `None` over a previously
        placed index (see `_save_alignment`'s "None must not wipe" discipline).
        """
        def _build_linear_fallback_map(reason: str) -> List[Dict]:
            end_ts = 0.0
            if segments:
                try:
                    end_ts = float(segments[-1].get('end', 0.0) or 0.0)
                except Exception:
                    end_ts = 0.0

            logger.warning(
                "⚠️ Anchor alignment failed (%s) — falling back to linear map. "
                "Sync will work but position accuracy may be reduced. "
                "Consider using a larger Whisper model.",
                reason,
            )
            return [
                {"char": 0, "ts": 0.0},
                {"char": len(full_text), "ts": max(0.0, end_ts)},
            ]

        def _fallback(reason: str) -> Tuple[List[Dict], str, None]:
            # Embedding anchor rescue fires only here — when lexical anchoring fails.
            # Never a segmented map — segments require successful lexical anchoring.
            rescue = self._embedding_anchor_rescue(segments, full_text)
            if rescue:
                return rescue, "llm_anchor", None
            return _build_linear_fallback_map(reason), "linear", None

        # 1. Tokenize Transcript
        transcript_words = []
        timed_word_count = 0
        for seg in segments:
            timed_tokens = self._timed_segment_tokens(seg)
            if timed_tokens:
                for token in timed_tokens:
                    transcript_words.append({**token, 'orig_index': len(transcript_words)})
                timed_word_count += len(timed_tokens)
                continue
            raw_words = seg['text'].split()
            if not raw_words: continue
            
            duration = seg['end'] - seg['start']
            per_word = duration / len(raw_words)
            
            for i, w in enumerate(raw_words):
                norm = self.polisher.normalize(w)
                if not norm: continue
                transcript_words.append({
                    "word": norm,
                    "ts": seg['start'] + (i * per_word),
                    "orig_index": len(transcript_words) # Keep track for slicing
                })

        logger.info("   Word timing: %s measured, %s estimated tokens",
                    timed_word_count, len(transcript_words) - timed_word_count)

        # 2. Tokenize Book
        book_words = []
        for match in re.finditer(r'\S+', full_text):
            raw_w = match.group()
            norm = self.polisher.normalize(raw_w)
            if not norm: continue
            book_words.append({
                "word": norm,
                "char": match.start(),
                "orig_index": len(book_words)
            })

        if not transcript_words or not book_words:
            return _fallback("insufficient normalized tokens")

        # --- Helper for N-Gram Logic ---
        def _find_anchors(t_tokens, b_tokens, n_size):
            # Build N-Grams
            def build_ngrams(items, is_book=False):
                grams = {}
                for i in range(len(items) - n_size + 1):
                    keys = [x['word'] for x in items[i:i+n_size]]
                    key = "_".join(keys)
                    if key not in grams: grams[key] = []
                    # Store entire object to retrieve ts/char/index
                    grams[key].append(items[i])
                return grams

            t_grams = build_ngrams(t_tokens, False)
            b_grams = build_ngrams(b_tokens, True)

            found = []
            for key, t_list in t_grams.items():
                if len(t_list) == 1: # Unique in transcript slice
                    if key in b_grams and len(b_grams[key]) == 1: # Unique in book slice
                        # Safe access using indices
                        b_item = b_grams[key][0]
                        t_item = t_list[0]

                        found.append({
                            "ts": t_item['ts'],
                            "char": b_item['char'],
                            "t_idx": t_item['orig_index'],
                            "b_idx": b_item['orig_index']
                        })
            return found

        # 3. PASS 1: Global Search (N=12)
        anchors = _find_anchors(transcript_words, book_words, n_size=_LEXICAL_ANCHOR_WORDS)

        # Sort by character position
        anchors.sort(key=lambda x: x['char'])

        # Segmented placement (issue #426 phase 2): fit each EPUB spine chapter to
        # the audio independently instead of forcing every candidate anchor into
        # one global increasing sequence. Gated so it can only ever take over from
        # the LIS below when it is actually needed: a book whose narration order
        # already matches its spine order is fully served by the LIS, so this
        # deliberately emits no segments for it (`map_segments` stays None,
        # `segments_json` stays NULL) — the flag can never change a map that was
        # already correct. See docs/PLAN_OUT_OF_ORDER_NARRATION.md.
        map_segments: Optional[List[Segment]] = None
        use_segmented = False
        if self.segmented_maps_enabled() and spine_chapters:
            boundaries = [(c['start'], c['end']) for c in spine_chapters if c['end'] > c['start']]
            if boundaries:
                placements = fit_segments(anchors, boundaries, total_chars=len(full_text))
                if placements:
                    # fit_segments already returns its result sorted by ts_start;
                    # compare that order against the same segments sorted by
                    # char_start to detect whether narration order actually
                    # differs from spine order.
                    by_char_start = sorted(placements, key=lambda seg: seg.char_start)
                    if placements != by_char_start:
                        use_segmented = True
                        map_segments = placements
                        logger.info(
                            "🧩 Alignment: segmented placement for %s — %d/%d spine boundaries "
                            "placed, narration order differs from spine order; using per-segment "
                            "anchors instead of the global LIS",
                            abs_id or "unknown", len(placements), len(boundaries),
                        )
                    else:
                        logger.info(
                            "🧩 Alignment: segmented placement for %s — %d/%d spine boundaries "
                            "placed but narration order matches spine order; keeping the global LIS",
                            abs_id or "unknown", len(placements), len(boundaries),
                        )
                else:
                    logger.info(
                        "🧩 Alignment: segmented placement for %s found no placeable spine "
                        "boundaries (of %d candidates); keeping the global LIS",
                        abs_id or "unknown", len(boundaries),
                    )

        if use_segmented:
            # Segmented replacement for the global LIS: retained anchors come
            # from the per-segment placements instead.
            valid_anchors = select_anchors(anchors, map_segments)
            logger.info(f"   📊 Segmented filter: {len(anchors)} candidates -> {len(valid_anchors)} valid")
        else:
            # Filter Monotonic (Global) — Longest Increasing Subsequence
            valid_anchors = self._filter_monotonic_lis(anchors)
            logger.info(f"   📊 Monotonic LIS filter: {len(anchors)} candidates -> {len(valid_anchors)} valid")
            if len(anchors) > len(valid_anchors):
                logger.info(f"      📊 Dropped {len(anchors) - len(valid_anchors)} non-monotonic anchors")

        # Diagnostics only (issue #426) — a large run of anchors the LIS could not
        # chain onto the retained subsequence is the signature of an EPUB spine
        # whose chapter/section order doesn't match the audiobook's narration
        # order (Four Past Midnight: a four-novella collection spined 2-4-3-1 but
        # narrated 1-2-3-4). This never gates or alters `valid_anchors` — a
        # detector failure must never break alignment.
        try:
            out_of_order_blocks = map_quality.detect_out_of_order_blocks(
                anchors, valid_anchors, total_chars=len(full_text))
            if out_of_order_blocks:
                dropped = len(anchors) - len(valid_anchors)
                dropped_pct = (100.0 * dropped / len(anchors)) if anchors else 0.0
                block_summary = "; ".join(
                    f"chars {block['char_start']}-{block['char_end']} / "
                    f"ts {block['ts_start']:.1f}-{block['ts_end']:.1f}s"
                    for block in out_of_order_blocks[:4]
                )
                logger.warning(
                    f"⚠️ Alignment: EPUB and audio are out of order for {abs_id or 'unknown'} — "
                    f"{dropped} non-monotonic anchors dropped ({dropped_pct:.1f}% of {len(anchors)} candidates), "
                    f"{len(out_of_order_blocks)} large out-of-order block(s): {block_summary}. "
                    "The EPUB's chapter/section order does not match the audiobook's narration "
                    "order, so positions inside these blocks will be wrong; a correctly ordered "
                    "EPUB is the fix."
                )
        except Exception:
            logger.warning("⚠️ Alignment: out-of-order block detection failed", exc_info=True)

        # 4. PASS 2: Backfill Start (N=6) "Work Backwards"
        # If the first anchor is significantly into the book, try to recover the intro.
        # Threshold: First anchor is > 1000 chars in AND > 30 seconds in
        # Skipped for the segmented path (issue #426 phase 2): `first['t_idx']` is
        # the anchor's position in audio-chronological order, not char order, so
        # for a segment placed late in the audio (e.g. front-matter narrated last)
        # this "late start" heuristic is backwards — it would slice almost the
        # *entire* transcript as "before the intro" and search it for garbage
        # early anchors instead of skipping cleanly.
        if (not use_segmented and valid_anchors
                and valid_anchors[0]['char'] > 1000 and valid_anchors[0]['ts'] > 30.0):
            first = valid_anchors[0]
            logger.info(f"   🔄 Late start detected (Char: {first['char']}, TS: {first['ts']:.1f}s) — Attempting backfill")

            # Slice the data: Everything BEFORE the first anchor
            # We use the indices we stored during tokenization
            t_slice = transcript_words[:first['t_idx']]
            b_slice = book_words[:first['b_idx']]

            if t_slice and b_slice:
                # Run with reduced N-Gram (N=6)
                # Lower N is risky globally, but safe in this small constrained window
                early_anchors = _find_anchors(t_slice, b_slice, n_size=6)
                
                # Filter Early Anchors (Must be monotonic with themselves)
                early_anchors.sort(key=lambda x: x['char'])
                valid_early = self._filter_monotonic_lis(early_anchors)
                
                if valid_early:
                    logger.info(f"   ✅ Backfill success: Recovered {len(valid_early)} early anchors.")
                    # Prepend to main list
                    valid_anchors = valid_early + valid_anchors



        # 5. Build Final Map
        final_map = []
        if not valid_anchors:
            return _fallback("no unique anchors found with N=12/N=6")

        if use_segmented:
            # No global 0.0/end_ts padding here: those force a single flat-map
            # slope from the very start/end of the *book* to the very start/end
            # of the *audio*, which is exactly the cross-segment interpolation
            # this feature exists to stop (a segment's own char_start/char_end
            # need not be narrated anywhere near ts 0 or the audio's end). The
            # lookup helpers (`get_time_for_text`/`_interpolate_char_for_time`)
            # already clamp to the nearest retained anchor for chars outside
            # `valid_anchors`' own range, which is correct because that anchor is
            # guaranteed to belong to the same (correct) segment.
            final_map.extend(valid_anchors)
        else:
            # Force 0,0 if still missing (Linear Interpolation fallback)
            if valid_anchors[0]['char'] > 0:
                final_map.append({"char": 0, "ts": 0.0})

            final_map.extend(valid_anchors)

            # Force End
            last = valid_anchors[-1]
            if last['char'] < len(full_text):
                # Safe check for segments
                end_ts = segments[-1]['end'] if segments else last['ts']
                final_map.append({"char": len(full_text), "ts": end_ts})

        logger.info(f"   ⚓ Anchored Alignment: Found {len(valid_anchors)} anchors (Total).")

        # Provenance: a map anchored on measured per-word timings ('lexical_timed')
        # is already word-accurate, so Remap must not offer to rebuild it as an
        # upgrade. Require a measured majority so a stray timed segment on an
        # otherwise estimated transcript does not mislabel the map.
        method = "lexical"
        if transcript_words and timed_word_count >= 0.5 * len(transcript_words):
            method = "lexical_timed"

        return final_map, method, map_segments

    # --- Embedding-assisted alignment (optional, gated; fires only when lexical fails) ---

    @staticmethod
    def _chunk_segments_to_windows(segments: List[Dict], max_windows: int) -> List[Dict]:
        """Group transcript segments into <= max_windows windows of {ts, text}."""
        usable = [s for s in segments if (s.get('text') or '').strip()]
        if not usable:
            return []
        group = max(1, -(-len(usable) // max_windows))  # ceil division
        windows = []
        for i in range(0, len(usable), group):
            chunk = usable[i:i + group]
            text = " ".join((s.get('text') or '').strip() for s in chunk).strip()
            if not text:
                continue
            try:
                ts = float(chunk[0].get('start', 0.0) or 0.0)
            except (TypeError, ValueError):
                ts = 0.0
            windows.append({"ts": ts, "text": text})
        return windows

    @staticmethod
    def _chunk_text_to_windows(full_text: str, max_windows: int) -> List[Dict]:
        """Slice the book text into <= max_windows windows of {char, text}."""
        n = len(full_text)
        if n <= 0:
            return []
        size = max(1, -(-n // max_windows))  # ceil division
        windows = []
        for start in range(0, n, size):
            text = full_text[start:start + size].strip()
            if text:
                windows.append({"char": start, "text": text})
        return windows

    def _embedding_anchor_rescue(self, segments: List[Dict], full_text: str) -> Optional[List[Dict]]:
        """Semantic anchor rescue: when lexical n-gram anchoring fails, embed transcript
        and book windows and place anchors at the best-matching pairs. Returns a map
        (>=2 anchors) or None to keep the linear fallback."""
        if not self._env_true("OLLAMA_ALIGN_ANCHOR_RESCUE") or not self._ollama_ready():
            return None
        if not segments or not full_text:
            return None

        max_windows = self._env_int("OLLAMA_ALIGN_MAX_WINDOWS", 80)
        threshold = self._env_float("OLLAMA_ALIGN_SIM_THRESHOLD", 0.72)

        t_windows = self._chunk_segments_to_windows(segments, max_windows)
        b_windows = self._chunk_text_to_windows(full_text, max_windows)
        if len(t_windows) < 2 or len(b_windows) < 2:
            return None

        # Embedding models silently truncate long inputs (nomic-embed-text caps at
        # ~2048 tokens); embed a bounded prefix so the cutoff point is known. The
        # window offsets are untouched — both sides compare co-located prefixes.
        cap = self._EMBED_WINDOW_MAX_CHARS
        t_texts = [w["text"][:cap] for w in t_windows]
        b_texts = [w["text"][:cap] for w in b_windows]
        vectors = self.ollama_client.embed(t_texts + b_texts)
        if not vectors or len(vectors) != len(t_texts) + len(b_texts):
            logger.info("   🧠 Anchor rescue skipped (embedding unavailable)")
            return None

        from src.api.ollama_client import cosine_similarity

        t_vecs = vectors[:len(t_texts)]
        b_vecs = vectors[len(t_texts):]

        anchors = []
        for t_win, t_vec in zip(t_windows, t_vecs):
            best_char = None
            best_cos = 0.0
            for b_win, b_vec in zip(b_windows, b_vecs):
                cos = cosine_similarity(t_vec, b_vec)
                if cos > best_cos:
                    best_cos = cos
                    best_char = b_win["char"]
            if best_char is not None and best_cos >= threshold:
                anchors.append({"char": best_char, "ts": t_win["ts"]})

        if len(anchors) < 2:
            return None

        anchors.sort(key=lambda a: a["char"])
        valid = self._filter_monotonic_lis(anchors)
        if len(valid) < 2:
            return None

        final_map = []
        if valid[0]["char"] > 0:
            final_map.append({"char": 0, "ts": 0.0})
        final_map.extend(valid)
        last = valid[-1]
        if last["char"] < len(full_text):
            try:
                end_ts = float(segments[-1].get("end", 0.0) or 0.0)
            except (TypeError, ValueError):
                end_ts = last["ts"]
            final_map.append({"char": len(full_text), "ts": max(end_ts, last["ts"])})

        logger.info(f"   🧠 Embedding anchor rescue: recovered {len(valid)} anchors.")
        return final_map

    @staticmethod
    def _sample_passages(text: str, count: int = 3, window: int = 400) -> List[str]:
        """Sample `count` passages spread across `text` (avoids opening boilerplate)."""
        n = len(text)
        if n <= 0:
            return []
        if n <= window:
            return [text.strip()] if text.strip() else []
        fractions = [0.1, 0.5, 0.9][:count]
        samples = []
        for f in fractions:
            start = min(max(0, int(n * f) - window // 2), n - window)
            chunk = text[start:start + window].strip()
            if chunk:
                samples.append(chunk)
        return samples

    def _verify_content_match(self, segments: List[Dict], full_text: str, abs_id: str = None) -> bool:
        """Content-match guard: refuse to store a map when audio and ebook are clearly
        different content. Returns True (proceed) unless the evidence shows strong
        divergence. No-op (True) when the master switch (OLLAMA_ALIGN_CONTENT_GUARD)
        is off, or when neither evidence path can render a verdict.

        Two independent evidence paths, in preference order:
        1. Embedding similarity (Ollama) -- used whenever a configured client is
           available. Unchanged from before issue #426's lexical fallback: same log
           line, same threshold (OLLAMA_ALIGN_CONTENT_MIN_SIM).
        2. Lexical n-gram overlap (`map_quality.transcript_text_overlap`) -- the
           non-LLM fallback used whenever the embedding path renders no verdict,
           covering both "no client configured" and "client configured but
           failing", gated by
           its own CONTENT_MATCH_GUARD switch so this guard is not a permanent
           no-op on installs without Ollama (issue #426: eight mismatched pairings
           were silently stored as maps on this repo's own live install before this
           existed).
        """
        if not self._env_true("OLLAMA_ALIGN_CONTENT_GUARD"):
            return True
        if not segments or not full_text:
            return True

        transcript_text = " ".join((s.get("text") or "").strip() for s in segments).strip()

        if self._ollama_ready():
            min_sim = self._env_float("OLLAMA_ALIGN_CONTENT_MIN_SIM", 0.45)
            t_samples = self._sample_passages(transcript_text)
            b_samples = self._sample_passages(full_text)
            vectors = None
            if t_samples and b_samples:
                vectors = self.ollama_client.embed(t_samples + b_samples)

            # Only a path that actually produced a similarity returns a verdict.
            # No sampleable passages, or embed() failing / returning malformed
            # vectors, falls through to the lexical check below instead of
            # returning True: a guard that silently disables itself whenever a
            # service is down is the failure mode this work exists to close.
            if vectors and len(vectors) == len(t_samples) + len(b_samples):
                from src.api.ollama_client import cosine_similarity

                t_vecs = vectors[:len(t_samples)]
                b_vecs = vectors[len(t_samples):]
                # Most-optimistic similarity: best book passage for the best transcript passage.
                best_overall = 0.0
                for t_vec in t_vecs:
                    for b_vec in b_vecs:
                        cos = cosine_similarity(t_vec, b_vec)
                        if cos > best_overall:
                            best_overall = cos

                if best_overall < min_sim:
                    logger.warning(
                        "🚫 Content-match guard: audio/ebook content diverges for %s "
                        "(best passage similarity %.2f < %.2f) — likely wrong edition / abridged / "
                        "translation / mis-match. Refusing to store a misleading alignment.",
                        abs_id or "?",
                        best_overall,
                        min_sim,
                    )
                    return False
                return True

        # Embedding path could not render a verdict -- no Ollama, no configured
        # client, or a configured client that failed. Non-LLM lexical fallback,
        # gated separately so a user who deliberately disabled the whole guard
        # (OLLAMA_ALIGN_CONTENT_GUARD, checked above) keeps that behaviour.
        # Note this covers an Ollama OUTAGE too: previously that meant no guard
        # at all, which is the same silent-garbage-map hole by another route.
        if not env_truthy("CONTENT_MATCH_GUARD", "true"):
            return True

        min_overlap = self._env_float("CONTENT_MATCH_MIN_OVERLAP", 0.15)
        overlap = map_quality.transcript_text_overlap(transcript_text, full_text)
        if overlap < min_overlap:
            logger.warning(
                "🚫 Lexical content-match guard: audio/ebook n-gram overlap too low for %s "
                "(%.1f%% of sampled ebook n-grams found in the transcript, need %.1f%%) — "
                "the audio and ebook do not appear to be the same work. Refusing to store "
                "a misleading alignment.",
                abs_id or "?",
                overlap * 100.0,
                min_overlap * 100.0,
            )
            return False
        return True

    def _publish_map(self, abs_id: str, alignment_map: List[Dict], align_method: str,
                     total_chars: Optional[int] = None,
                     exclude_spans: Optional[List[Tuple[int, int]]] = None,
                     segments: Optional[List[Segment]] = None) -> bool:
        """Store `alignment_map` unless doing so would regress a materially better incumbent.

        `segments` (issue #426 phase 2) is the per-chapter placement index to
        persist alongside the map, or `None` to leave any existing index
        untouched — see `_save_alignment`'s "None must not wipe" discipline;
        only `align_and_store` ever supplies a non-`None` value.

        Scoring (issue #426 phase 3): the challenger is scored with its own
        `segments` and the incumbent with whatever segment index is already
        stored for `abs_id` (`_get_segments`) — never each other's. The two
        maps can disagree on whether they're segmented at all (a fresh
        segmented re-align challenging a legacy flat incumbent, or vice
        versa), so mixing them up would score at least one side against a
        segment index it doesn't structurally match.

        Every alignment write funnels through this seam (issue #426). Previously only
        the CTC path backed up and checked anything before overwriting the stored map,
        so re-aligning a book that already had a good CTC map was silently destroyed by
        a fresh lexical map before anything decided the lexical one was better (Four
        Past Midnight: the CTC map scored 0.324, the lexical rebuild that clobbered it
        scored 0.200).

        This is deliberately a regression *veto*, not a "challenger must be better"
        test: on a healthy book a CTC map and a lexical map score almost identically
        (~0.996 on live data), so requiring the challenger to score higher would reject
        nearly every healthy CTC upgrade and silently disable that path entirely. The
        score is only trustworthy as a detector of material degradation, never as a
        tie-breaker between two maps that both look healthy (see
        `map_quality.is_regression`).

        Returns True when the map was stored, False when the write was vetoed as a
        regression — the existing map is left untouched and no backup is taken.
        """
        challenger_quality = map_quality.score_map(alignment_map, exclude_spans, segments=segments)
        incumbent = self._get_alignment(abs_id)
        incumbent_segments = self._get_segments(abs_id)
        incumbent_method = self.database_service.get_alignment_method(abs_id) or ""
        incumbent_total_chars = self._get_alignment_total_chars(abs_id)

        skip_veto = (
            not incumbent or len(incumbent) < 2
            or incumbent_method in self._CTC_REPLACEABLE_METHODS
            or (total_chars is not None and incumbent_total_chars is not None
                and incumbent_total_chars != total_chars)
            # An incumbent with no recorded total_chars (84% of stored maps predate
            # that field) cannot prove which ebook it was built against, so its score
            # is not a trustworthy comparison basis against a challenger that does
            # know its own ebook length -- this can never make a book worse than the
            # pre-veto behaviour, which always overwrote unconditionally. Every CTC
            # map (the regression this veto exists for) records total_chars, so the
            # protection that matters is unaffected.
            or (total_chars is not None and incumbent_total_chars is None)
        )
        if not skip_veto:
            incumbent_quality = map_quality.score_map(incumbent, exclude_spans, segments=incumbent_segments)
            if map_quality.is_regression(incumbent_quality, challenger_quality):
                logger.warning(
                    "🚫 Map publish vetoed for %s — challenger '%s' scores %.3f "
                    "(density_spread=%.2f, max_gap_fraction=%.3f) vs incumbent '%s' %.3f "
                    "(density_spread=%.2f, max_gap_fraction=%.3f); keeping the existing map",
                    abs_id, align_method, challenger_quality.score,
                    challenger_quality.density_spread, challenger_quality.max_gap_fraction,
                    incumbent_method or "existing", incumbent_quality.score,
                    incumbent_quality.density_spread, incumbent_quality.max_gap_fraction,
                )
                return False

        self._backup_alignment(abs_id)
        self._save_alignment(abs_id, alignment_map, align_method, total_chars=total_chars,
                             quality=challenger_quality, segments=segments)
        return True

    def _save_alignment(self, abs_id: str, alignment_map: List[Dict], align_method: str = None,
                        total_chars: Optional[int] = None,
                        quality: Optional[map_quality.MapQuality] = None,
                        segments: Optional[List[Segment]] = None):
        """Upsert alignment to SQLite."""
        quality_score = quality.score if quality is not None else None
        quality_detail = map_quality.quality_detail_json(quality) if quality is not None else None
        segments_json = _segments_to_json(segments) if segments is not None else None
        with self.database_service.get_session() as session:
            json_blob = json.dumps(alignment_map)

            # Check exist
            existing = session.query(BookAlignment).filter_by(abs_id=abs_id).first()
            if existing:
                existing.alignment_map_json = json_blob
                existing.align_method = align_method
                # Only update total_chars when caller supplies a value; a map rebuilt
                # against the same ebook keeps the same length, and a caller that
                # simply doesn't know the length (e.g. unanchored Storyteller path)
                # must not destroy a known-good value.
                if total_chars is not None:
                    existing.total_chars = total_chars
                # Same discipline for quality: a caller that didn't score the map
                # (e.g. a direct restore) must not wipe a previously recorded score.
                if quality is not None:
                    existing.quality_score = quality_score
                    existing.quality_detail = quality_detail
                # Segments are NOT metadata and get the OPPOSITE discipline
                # (issue #426). `total_chars` and `quality` describe a map a
                # caller may legitimately not have measured, so a None there
                # preserves what is stored. `segments_json` describes *this*
                # map's own layout, and this method always replaces
                # `alignment_map_json`, so carrying a previous map's segments
                # forward would pair one map's flat points with another map's
                # segment boundaries -- silently mis-resolving every lookup.
                # The concrete route: an out-of-order book stores segments from
                # the lexical stage, then the CTC upgrade overwrites the map
                # without any (`_publish_map` at the 'ctc' call site passes
                # none). Always write them, so map and segments are replaced
                # together or not at all.
                existing.segments_json = segments_json
                existing.last_updated = utcnow()
            else:
                new_align = BookAlignment(abs_id=abs_id, alignment_map_json=json_blob,
                                          align_method=align_method, total_chars=total_chars,
                                          quality_score=quality_score, quality_detail=quality_detail,
                                          segments_json=segments_json)
                session.add(new_align)

            # Context manager handles commit
            logger.info(f"   💾 Saved alignment for {abs_id} to DB.")
        self._alignment_cache.delete(abs_id)
        self._total_chars_cache.pop(abs_id, None)
        self._segments_cache.pop(abs_id, None)

    def _backup_alignment(self, abs_id: str) -> bool:
        """Copy a book's current stored map into the backup table before it is
        overwritten (issue #426). No-op (returns False) when it has no map yet."""
        with self.database_service.get_session() as session:
            current = session.query(BookAlignment).filter_by(abs_id=abs_id).first()
            if not current:
                return False
            backup = session.query(BookAlignmentBackup).filter_by(abs_id=abs_id).first()
            if backup:
                backup.alignment_map_json = current.alignment_map_json
                backup.align_method = current.align_method
                backup.total_chars = current.total_chars
                backup.segments_json = current.segments_json
                backup.backed_up_at = utcnow()
            else:
                session.add(BookAlignmentBackup(
                    abs_id=abs_id,
                    alignment_map_json=current.alignment_map_json,
                    align_method=current.align_method,
                    total_chars=current.total_chars,
                    segments_json=current.segments_json,
                ))
        return True

    def restore_previous_alignment(self, abs_id: str) -> bool:
        """Restore the map replaced by the last CTC overwrite (issue #426).

        Copies the backup map back over the current one; the backup is kept so a
        restore can be repeated. Returns False when there is nothing to restore.
        """
        with self.database_service.get_session() as session:
            backup = session.query(BookAlignmentBackup).filter_by(abs_id=abs_id).first()
            if not backup:
                return False
            method = backup.align_method
            current = session.query(BookAlignment).filter_by(abs_id=abs_id).first()
            if current:
                current.alignment_map_json = backup.alignment_map_json
                current.align_method = backup.align_method
                current.total_chars = backup.total_chars
                current.segments_json = backup.segments_json
                current.last_updated = utcnow()
            else:
                session.add(BookAlignment(
                    abs_id=abs_id,
                    alignment_map_json=backup.alignment_map_json,
                    align_method=backup.align_method,
                    total_chars=backup.total_chars,
                    segments_json=backup.segments_json,
                ))
        self._alignment_cache.delete(abs_id)
        self._total_chars_cache.pop(abs_id, None)
        self._segments_cache.pop(abs_id, None)
        logger.info(
            "↩️ Restored previous alignment for %s (method '%s')", abs_id, method or "unknown",
        )
        return True

    def _get_alignment_total_chars(self, abs_id: str) -> Optional[int]:
        """Cached ebook length for a book's map; None when the map predates it.

        Coerces defensively: a non-numeric value must degrade to the legacy
        last-anchor denominator rather than poison the comparison below it.
        """
        if abs_id in self._total_chars_cache:
            return self._total_chars_cache[abs_id]
        try:
            total_chars = int(self.database_service.get_alignment_total_chars(abs_id))
        except (AttributeError, TypeError, ValueError):
            total_chars = None
        self._total_chars_cache[abs_id] = total_chars
        return total_chars

    def get_map_terminal_char(self, abs_id: str) -> Optional[int]:
        """The highest character offset this book's alignment map anchors.

        This is a fingerprint of the EPUB the map was fitted against, and unlike
        `total_chars` it cannot be contaminated after the fact: it is the stored
        map's own last anchor, while `total_chars` is backfilled by callers from
        whichever EPUB they happened to be holding.

        `SyncManager._get_alignment_epub_filename` compares it against candidate
        EPUB lengths to decide whose character space the map speaks. Map points
        are stored sorted by char, so the last point carries the maximum.
        """
        alignment = self._get_alignment(abs_id)
        if not alignment:
            return None
        try:
            return self._point_char(alignment[-1])
        except (IndexError, TypeError, ValueError):
            return None

    def record_total_chars_if_missing(self, abs_id: str, total_chars: int) -> bool:
        """Backfill the ebook length for a map stored before it was captured.

        Every map written before the column existed divides by its own last anchor
        instead of the book's length, which over-reports the position it reports
        back. Re-aligning is the only other way to heal one, so callers that
        already hold the ebook text pass its length here; it is a no-op once
        recorded, and never overwrites an existing value.

        Returns whether a value was written.
        """
        if not abs_id or not total_chars or total_chars <= 0:
            return False
        # A cached non-None means it is already recorded; skip the DB round-trip.
        if self._total_chars_cache.get(abs_id) is not None:
            return False
        try:
            wrote = self.database_service.set_alignment_total_chars_if_missing(
                abs_id, int(total_chars)
            )
        except Exception as e:
            logger.warning(
                f"⚠️ Could not backfill alignment length for '{abs_id}': {e}", exc_info=True
            )
            return False
        if wrote:
            self._total_chars_cache[abs_id] = int(total_chars)
            logger.info(
                "📏 Backfilled alignment length for '%s' (%d chars) — its stored map "
                "predates the ebook-length column and was reporting positions against "
                "its own last anchor",
                abs_id, int(total_chars),
            )
        return wrote

    def _get_alignment(self, abs_id: str) -> Optional[List[Dict]]:
        # A long book's map is a 10-15MB JSON blob, and this runs several
        # times per sync cycle (duration, normalization, locator mapping) —
        # re-reading and re-parsing it each call dominated cycle time. All
        # callers treat the returned list as read-only, so sharing the cached
        # object is safe. Invalidated on _save_alignment; an entry whose row
        # is deleted outside the service (book unlink/delete) lingers but is
        # unreachable until a re-alignment stores a fresh map.
        cached = self._alignment_cache.get(abs_id)
        if cached is not None:
            return cached
        with self.database_service.get_session() as session:
            entry = session.query(BookAlignment).filter_by(abs_id=abs_id).first()
            if entry:
                alignment = json.loads(entry.alignment_map_json)
                self._alignment_cache.put(abs_id, alignment)
                return alignment
            return None

    def _get_segments(self, abs_id: str) -> Optional[List[Dict]]:
        """Cached segment placement index for one book's map (issue #426
        phase 1) -- the `[{char_start, char_end, ts_start, ts_end}, ...]`
        list from `segments_json`, or None when it's NULL (the flat
        monotonic legacy path).

        Unlike `_alignment_cache` (an `LRUCache`, where "not yet cached" and
        "cached as nothing" are both just absence, since a missing DB row is
        deliberately never cached there), "no segments" is itself a
        frequent, valid, repeatedly-asked answer here -- every map without
        out-of-order narration has one -- so it must be cached too, or every
        lookup on a NULL-segments book pays a DB round-trip it would
        otherwise skip. A plain dict that distinguishes "key absent" from
        "value is None" does that, mirroring `_total_chars_cache`. Invalidated
        alongside `_alignment_cache` in `_save_alignment` and
        `restore_previous_alignment` -- the only two sites that write
        `book_alignments` rows.

        The stored column is always `None` or a JSON string; anything else
        (a test double standing in for the row, a corrupt value) degrades to
        `None` rather than raising, the same spirit as
        `_get_alignment_total_chars`'s defensive coercion.
        """
        if abs_id in self._segments_cache:
            return self._segments_cache[abs_id]
        with self.database_service.get_session() as session:
            entry = session.query(BookAlignment).filter_by(abs_id=abs_id).first()
            if entry is None:
                return None
            raw = entry.segments_json
            segments = json.loads(raw) if isinstance(raw, str) and raw else None
            self._segments_cache[abs_id] = segments
            return segments

    def get_book_duration(self, abs_id: str) -> Optional[float]:
        """Get the total duration of the book from its alignment map."""
        alignment = self._get_alignment(abs_id)
        if alignment and len(alignment) > 0:
            # The last point in the alignment map should have the max timestamp
            return float(alignment[-1]['ts'])
        return None


# ---------------------------------------------------------------------------
# Storyteller transcript ingestion helpers (used by web_server and forge_service)
# ---------------------------------------------------------------------------

from src.utils.logging_utils import sanitize_log_data as _sanitize_log_data


def _normalize_title_key(title: str) -> str:
    """Normalize title for deterministic directory matching."""
    lowered = (title or "").lower()
    collapsed = re.sub(r"[^a-z0-9]+", " ", lowered)
    return re.sub(r"\s+", " ", collapsed).strip()


def _strip_storyteller_instance_suffix(name: str) -> str:
    stripped = str(name or "").strip()
    return re.sub(r"\s+\[[^\[\]]+\]\s*$", "", stripped).strip()


def _storyteller_dir_has_transcriptions(title_dir: Path) -> bool:
    transcriptions_dir = Path(title_dir) / "transcriptions"
    return transcriptions_dir.is_dir() and any(transcriptions_dir.glob("*.json"))


def _iter_storyteller_title_dir_candidates(assets_dir: Path, target_title: str) -> list[Path]:
    target_key = _normalize_title_key(target_title)
    if not target_key:
        return []

    candidates = []
    for child in assets_dir.iterdir():
        if not child.is_dir():
            continue
        child_key = _normalize_title_key(child.name)
        base_key = _normalize_title_key(_strip_storyteller_instance_suffix(child.name))
        if child.name == target_title or child_key == target_key or base_key == target_key:
            candidates.append(child)
    return candidates


def _storyteller_filename_for_abs_chapter(chapter_index: int, prefix: str = "00000") -> str:
    """
    Build the bridge-managed canonical chapter filename for ABS chapter index N (0-based).

    This helper is for destination naming only (managed transcript store), not for
    source layout detection inside Storyteller asset folders.
    """
    return f"{prefix}-{chapter_index + 1:05d}.json"


def _resolve_storyteller_title_dir(
    assets_root: Path,
    abs_title: str,
    storyteller_title: str = None,
) -> Optional[Path]:
    """
    Resolve the Storyteller title directory, preferring transcript-ready
    directories and supporting Storyteller's `Title [id]` suffix pattern.
    """
    assets_dir = assets_root / "assets"
    if not assets_dir.exists() or not assets_dir.is_dir():
        return None

    candidates: list[Path] = []
    seen = set()
    raw_titles = []
    if storyteller_title:
        raw_titles.append(storyteller_title)
    if abs_title and abs_title not in raw_titles:
        raw_titles.append(abs_title)

    for target_title in raw_titles:
        for candidate in _iter_storyteller_title_dir_candidates(assets_dir, target_title):
            resolved = str(candidate.resolve())
            if resolved in seen:
                continue
            seen.add(resolved)
            candidates.append(candidate)

    if not candidates:
        return None

    transcript_ready = [candidate for candidate in candidates if _storyteller_dir_has_transcriptions(candidate)]
    if transcript_ready:
        ignored = [candidate for candidate in candidates if candidate not in transcript_ready]
        for candidate in ignored:
            logger.info(
                "Storyteller transcript resolver: ignoring stale non-transcription dir '%s'",
                candidate,
            )
        candidates = transcript_ready

    if len(candidates) == 1:
        selected = candidates[0]
        if _strip_storyteller_instance_suffix(selected.name) != selected.name:
            logger.info(
                "Storyteller transcript resolver: selected suffixed assets dir '%s' for '%s'",
                selected,
                _sanitize_log_data(storyteller_title or abs_title),
            )
        return selected

    for target_title in [storyteller_title, abs_title]:
        if not target_title:
            continue
        exact_matches = [candidate for candidate in candidates if candidate.name == target_title]
        if len(exact_matches) == 1:
            return exact_matches[0]

    if len(candidates) > 1:
        logger.warning(
            "Storyteller transcript resolver: ambiguous transcript-ready matches for '%s' (%d directories)",
            _sanitize_log_data(storyteller_title or abs_title),
            len(candidates),
        )
    return None


def _is_storyteller_wordtimeline_chapter(chapter_path: Path) -> bool:
    try:
        with open(chapter_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return False
        if isinstance(data.get("wordTimeline"), list):
            return True
        return isinstance(data.get("timeline"), list)
    except Exception:
        return False


def _validate_storyteller_chapters(
    transcriptions_dir: Path,
) -> tuple[bool, list[str], list[str]]:
    """
    Validate Storyteller chapter files by naming pattern and structural validity.
    Accept known source layouts:
      1) 00000-00001 ... 00000-N
      2) 00001-00001 ... 00001-N
      3) 00000-00001, 00001-00001 ... (N-1)-00001
      4) 00001-00001, 00002-00001 ... N-00001
    Works with whatever valid files are present — no count expectation.
    Returns (is_valid, source_filenames, destination_filenames).
    """
    pattern = re.compile(r"^(\d{5})-(\d{5})\.json$")
    numeric_matches = []
    for p in transcriptions_dir.glob("*.json"):
        match = pattern.match(p.name)
        if match:
            numeric_matches.append((p.name, int(match.group(1)), int(match.group(2))))

    actual_count = len(numeric_matches)
    if actual_count <= 0:
        all_json = sorted([p.name for p in transcriptions_dir.glob("*.json")])
        logger.info(
            "Storyteller validation failed at '%s': no files matching pattern '^\\d{5}-\\d{5}\\.json$' found "
            "(total json=%d)",
            transcriptions_dir,
            len(all_json),
        )
        if all_json:
            sample = ", ".join(all_json[:10])
            logger.info(
                "Storyteller validation file sample at '%s': %s%s",
                transcriptions_dir,
                sample,
                " ..." if len(all_json) > 10 else "",
            )
        return False, [], []

    dest_files = [_storyteller_filename_for_abs_chapter(i, "00000") for i in range(actual_count)]

    candidate_layouts: list[tuple[str, list[str]]] = [
        (
            "prefix_00000",
            [f"00000-{i + 1:05d}.json" for i in range(actual_count)],
        ),
        (
            "prefix_00001",
            [f"00001-{i + 1:05d}.json" for i in range(actual_count)],
        ),
        (
            "chapter_first_zero_based",
            [f"{i:05d}-00001.json" for i in range(actual_count)],
        ),
        (
            "chapter_first_one_based",
            [f"{i + 1:05d}-00001.json" for i in range(actual_count)],
        ),
    ]

    for layout_name, source_files in candidate_layouts:
        if not all((transcriptions_dir / name).exists() for name in source_files):
            continue
        invalid_files = [
            name for name in source_files
            if not _is_storyteller_wordtimeline_chapter(transcriptions_dir / name)
        ]
        if not invalid_files:
            return True, source_files, dest_files
        logger.info(
            "Storyteller validation failed at '%s': layout '%s' has %d chapter file(s) without storyteller "
            "timeline format ('wordTimeline' or 'timeline'); first invalid='%s'",
            transcriptions_dir,
            layout_name,
            len(invalid_files),
            invalid_files[0],
        )
        return False, [], []

    all_json = sorted([p.name for p in transcriptions_dir.glob("*.json")])
    first_slot_values = [first for _, first, _ in numeric_matches]
    second_slot_values = [second for _, _, second in numeric_matches]
    logger.info(
        "Storyteller validation failed at '%s': no supported filename layout matched actual_count=%d",
        transcriptions_dir,
        actual_count,
    )
    if all_json:
        sample = ", ".join(all_json[:10])
        logger.info(
            "Storyteller validation file sample at '%s': %s%s",
            transcriptions_dir,
            sample,
            " ..." if len(all_json) > 10 else "",
        )
    if numeric_matches:
        logger.info(
            "Storyteller validation slot ranges at '%s': first_slot=%d..%d second_slot=%d..%d",
            transcriptions_dir,
            min(first_slot_values),
            max(first_slot_values),
            min(second_slot_values),
            max(second_slot_values),
        )
    return False, [], []


def _read_storyteller_chapter_metrics(chapter_file_path: Path) -> tuple[int, int, float]:
    """Return transcript lengths and chapter-local duration for a storyteller chapter file."""
    text_len = 0
    text_len_utf16 = 0
    local_duration = 0.0

    if not chapter_file_path.exists():
        return text_len, text_len_utf16, local_duration

    try:
        with open(chapter_file_path, "r", encoding="utf-8") as chapter_file:
            chapter_data = json.load(chapter_file)
        if not isinstance(chapter_data, dict):
            return text_len, text_len_utf16, local_duration

        chapter_text = chapter_data.get("transcript", "")
        text_len = len(chapter_text)
        text_len_utf16 = len(chapter_text.encode("utf-16-le")) // 2

        timeline = chapter_data.get("wordTimeline")
        if not isinstance(timeline, list):
            timeline = chapter_data.get("timeline")
        if isinstance(timeline, list):
            for row in timeline:
                if not isinstance(row, dict):
                    continue
                try:
                    end_time = float(row.get("endTime", 0.0) or 0.0)
                except (TypeError, ValueError):
                    end_time = 0.0
                if end_time > local_duration:
                    local_duration = end_time
    except Exception:
        return 0, 0, 0.0

    return text_len, text_len_utf16, local_duration


def probe_storyteller_transcripts(
    abs_title: str,
    chapters: list,
    storyteller_title: str = None,
) -> dict:
    """
    Non-mutating readiness probe for Storyteller transcript assets.
    """
    result = {
        "ready": False,
        "reason": "unknown",
        "transcriptions_dir": None,
        "expected_count": 0,
        "found_count": 0,
        "source_files": [],
        "expected_files": [],
        "chapterless_mode": False,
        "audio_aligned": False,
    }

    assets_dir_raw = os.environ.get("STORYTELLER_ASSETS_DIR", "").strip()
    if not assets_dir_raw:
        result["ready"] = True
        result["reason"] = "assets_not_configured"
        return result

    chapter_list = chapters if isinstance(chapters, list) else []
    assets_root = Path(assets_dir_raw)
    assets_search_root = assets_root / "assets"
    title_dir = _resolve_storyteller_title_dir(
        assets_root,
        abs_title or "",
        storyteller_title=storyteller_title,
    )
    if not title_dir:
        search_root_exists = assets_search_root.exists()
        search_root_is_dir = assets_search_root.is_dir()
        available_dirs = []
        if search_root_exists and search_root_is_dir:
            try:
                available_dirs = sorted(
                    child.name for child in assets_search_root.iterdir() if child.is_dir()
                )
            except Exception as list_err:
                logger.debug(
                    "Storyteller transcript probe could not list assets root '%s': %s",
                    assets_search_root,
                    list_err,
                )

        sample_dirs = available_dirs[:5]
        logger.info(
            "Storyteller transcript probe title_dir_missing: search_root='%s' exists=%s is_dir=%s "
            "abs_title='%s' storyteller_title='%s' available_dirs=%s total_dirs=%d",
            assets_search_root,
            search_root_exists,
            search_root_is_dir,
            _sanitize_log_data(abs_title),
            _sanitize_log_data(storyteller_title or ""),
            sample_dirs,
            len(available_dirs),
        )
        result["reason"] = "title_dir_missing"
        return result

    transcriptions_dir = title_dir / "transcriptions"
    result["transcriptions_dir"] = transcriptions_dir
    if not transcriptions_dir.exists() or not transcriptions_dir.is_dir():
        result["reason"] = "transcriptions_dir_missing"
        return result

    numeric_pattern = re.compile(r"^\d{5}-\d{5}\.json$")
    numeric_files = [p.name for p in transcriptions_dir.glob("*.json") if numeric_pattern.match(p.name)]
    result["found_count"] = len(numeric_files)

    chapterless_mode = len(chapter_list) <= 0
    result["chapterless_mode"] = chapterless_mode
    if chapterless_mode and len(numeric_files) <= 0:
        result["reason"] = "chapter_set_incomplete"
        return result

    result["expected_count"] = len(chapter_list) if not chapterless_mode else len(numeric_files)

    is_valid, source_files, expected_files = _validate_storyteller_chapters(transcriptions_dir)
    result["source_files"] = source_files
    result["expected_files"] = expected_files
    if not is_valid:
        result["reason"] = "chapter_set_incomplete"
        return result

    result["ready"] = True
    result["reason"] = "validated"
    if not chapterless_mode and len(source_files) != len(chapter_list):
        result["audio_aligned"] = True
    return result


def ingest_storyteller_transcripts(
    abs_id: str,
    abs_title: str,
    chapters: list,
    storyteller_title: str = None,
) -> Optional[str]:
    """
    Copy Storyteller chapter JSON files into bridge-managed data storage and write a manifest.
    Returns manifest path on success.
    """
    probe = probe_storyteller_transcripts(
        abs_title,
        chapters,
        storyteller_title=storyteller_title,
    )
    if probe["reason"] == "assets_not_configured":
        return None
    if probe["reason"] == "title_dir_missing":
        logger.info(f"Storyteller transcripts not found for '{abs_id}' (title='{_sanitize_log_data(abs_title)}')")
        return None
    if probe["reason"] == "transcriptions_dir_missing":
        transcriptions_dir = probe["transcriptions_dir"]
        logger.info(f"Storyteller transcriptions directory missing for '{abs_id}' at '{transcriptions_dir}'")
        return None
    if not probe["ready"]:
        transcriptions_dir = probe["transcriptions_dir"]
        expected_count = probe["expected_count"]
        logger.info(
            f"Storyteller transcripts rejected for '{abs_id}': expected {expected_count} chapter files at "
            f"'{transcriptions_dir}'"
        )
        return None

    chapter_list = chapters if isinstance(chapters, list) else []
    transcriptions_dir = probe["transcriptions_dir"]
    expected_count = probe["expected_count"]
    source_files = probe["source_files"]
    expected_files = probe["expected_files"]
    chapterless_mode = probe["chapterless_mode"]
    audio_aligned = probe.get("audio_aligned", False)

    if chapterless_mode:
        logger.info(
            f"Storyteller ingest chapterless mode for '{abs_id}': deriving {expected_count} chapters from "
            f"'{transcriptions_dir}'"
        )
    elif audio_aligned:
        logger.info(
            f"Storyteller ingest audio_aligned mode for '{abs_id}': file count ({len(source_files)}) differs "
            f"from ABS chapter count ({len(chapter_list)}), deriving timing from file contents"
        )

    data_dir = Path(os.environ.get("DATA_DIR", "/data"))
    target_dir = data_dir / "transcripts" / "storyteller" / abs_id
    target_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = target_dir / "manifest.json"

    existing_json_files = [p.name for p in target_dir.glob("*.json") if re.match(r"^00000-\d{5}\.json$", p.name)]
    existing_valid = (
        manifest_path.exists()
        and len(existing_json_files) == expected_count
        and all((target_dir / name).exists() for name in expected_files)
    )
    if existing_valid:
        logger.info(f"Storyteller ingest reuse for '{abs_id}' from '{target_dir}' ({len(expected_files)} files)")
    else:
        # Ensure stale canonical files are not mixed with a newly copied set.
        for stale_file in target_dir.glob("*.json"):
            if re.match(r"^00000-\d{5}\.json$", stale_file.name):
                try:
                    stale_file.unlink()
                except Exception as delete_err:
                    logger.warning(
                        "Storyteller ingest could not remove stale transcript '%s' for '%s': %s",
                        stale_file,
                        abs_id,
                        delete_err,
                        exc_info=True,
                    )
        copied_count = 0
        for source_name, target_name in zip(source_files, expected_files):
            shutil.copy2(transcriptions_dir / source_name, target_dir / target_name)
            copied_count += 1
        logger.info(
            f"Storyteller ingest copied for '{abs_id}': {copied_count} files from "
            f"'{transcriptions_dir}' to '{target_dir}'"
        )

    chapter_entries = []
    if chapterless_mode or audio_aligned:
        cumulative_start = 0.0
        for idx, chapter_file_name in enumerate(expected_files):
            chapter_file_path = target_dir / chapter_file_name
            text_len, text_len_utf16, local_duration = _read_storyteller_chapter_metrics(chapter_file_path)
            start = cumulative_start
            end = cumulative_start + max(0.0, float(local_duration))
            cumulative_start = end
            chapter_entries.append({
                "index": idx,
                "file": chapter_file_name,
                "start": start,
                "end": end,
                "text_len": text_len,
                "text_len_utf16": text_len_utf16,
            })
    else:
        for idx, chapter in enumerate(chapter_list):
            start = float(chapter.get("start", 0.0) or 0.0)
            end = float(chapter.get("end", 0.0) or 0.0)
            chapter_file_name = _storyteller_filename_for_abs_chapter(idx)
            chapter_file_path = target_dir / chapter_file_name
            text_len, text_len_utf16, _local_duration = _read_storyteller_chapter_metrics(chapter_file_path)
            chapter_entries.append({
                "index": idx,
                "file": chapter_file_name,
                "start": start,
                "end": end,
                "text_len": text_len,
                "text_len_utf16": text_len_utf16,
            })

    duration = 0.0
    if chapter_entries:
        duration = float(chapter_entries[-1].get("end", 0.0) or 0.0)

    manifest = {
        "format": "storyteller_manifest",
        "version": 1,
        "abs_id": abs_id,
        "abs_title": abs_title,
        "duration": duration,
        "chapter_count": expected_count,
        "chapters": chapter_entries
    }
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False)

    return str(manifest_path)
