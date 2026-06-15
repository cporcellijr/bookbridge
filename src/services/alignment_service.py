"""
Alignment Service.
Handles the core logic for aligning ebook text with audio transcriptions
and storing the results in the database.
"""

import bisect
import json
import logging
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional, Tuple

from src.db.models import BookAlignment
from src.utils.polisher import Polisher
from src.utils.logging_utils import time_execution

logger = logging.getLogger(__name__)

class AlignmentService:
    # Max chars of a window sent to the embedder (~1000 tokens, safely under
    # nomic-embed-text's 2048-token context).
    _EMBED_WINDOW_MAX_CHARS = 4000

    def __init__(self, database_service, polisher: Polisher, ollama_client=None):
        self.database_service = database_service
        self.polisher = polisher
        self.ollama_client = ollama_client

    def _ollama_ready(self) -> bool:
        client = self.ollama_client
        return bool(client and client.is_configured())

    @staticmethod
    def _env_true(key: str, default: str = "true") -> bool:
        return os.environ.get(key, default).lower() == "true"

    @staticmethod
    def _env_float(key: str, default: float) -> float:
        try:
            return float(os.environ.get(key, default))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _env_int(key: str, default: int) -> int:
        try:
            return int(float(os.environ.get(key, default)))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _point_char(point: Dict) -> int:
        if 'global_char' in point:
            return int(point['global_char'])
        return int(point.get('char', 0))

    @time_execution
    def align_and_store(self, abs_id: str, raw_segments: List[Dict], ebook_text: str, spine_chapters: List[Dict] = None):
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
        # Fix fragmented sentences (Mr. Smith case)
        # We pass ebook_text to help (though rebuild_fragmented_sentences uses simple heuristics currently)
        rebuilt_segments = self.polisher.rebuild_fragmented_sentences(raw_segments, ebook_text)
        logger.info(f"   Rebuilt segments: {len(raw_segments)} -> {len(rebuilt_segments)}")

        # 2b. Content-match guard — refuse to persist a map when the audio and ebook are
        # clearly different content (wrong edition / abridged / translation / mis-match).
        if not self._verify_content_match(rebuilt_segments, ebook_text, abs_id=abs_id):
            return False

        # 3. Anchored Alignment
        alignment_map, align_method = self._generate_alignment_map_with_method(rebuilt_segments, ebook_text)

        if not alignment_map:
            logger.error("   ❌ Failed to generate alignment map.")
            return False

        # 4. Store to Database
        self._save_alignment(abs_id, alignment_map, align_method)
        return True

    @time_execution
    def align_storyteller_and_store(self, abs_id: str, storyteller_transcript, ebook_text: str = None) -> bool:
        """
        Build a chapter-aware alignment map directly from Storyteller wordTimeline data,
        anchored to the actual EPUB text to prevent global offset drifts.
        """
        raw_segments = []
        for point in storyteller_transcript.iter_alignment_points():
            pass

        for chapter_index, meta in enumerate(storyteller_transcript.chapters):
            try:
                chapter = storyteller_transcript._load_chapter(chapter_index)
                chapter_start = float(meta.get("start", 0.0) or 0.0)
                
                for word_data in chapter.get("word_timeline", []):
                    text = word_data.get('text')
                    if not text:
                         start_utf16 = word_data.get('startOffsetUtf16', 0)
                         length_utf16 = word_data.get('lengthUtf16', 0)
                         pass
            except Exception:
                pass

        if ebook_text:
            logger.info(f"AlignmentService: Anchoring Storyteller transcript for {abs_id} to {len(ebook_text)} chars of text...")
            
            segments = []
            
            for point in storyteller_transcript.iter_alignment_points():
                pass

            # iter_alignment_points yields only timestamps/offsets; build text segments from chapter transcripts.
            for chapter_index, meta in enumerate(storyteller_transcript.chapters):
                try:
                    chapter = storyteller_transcript._load_chapter(chapter_index)
                    chapter_start = float(meta.get("start", 0.0) or 0.0)
                    transcript_text = chapter.get("transcript", "")
                    timeline = chapter.get("word_timeline", [])
                    
                    if not timeline or not transcript_text: continue
                    
                    # Group words into ~5s segments
                    seg_start = chapter_start + float(timeline[0].get("startTime", 0.0))
                    seg_text_words = []
                    
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
                        
                        # Break segment every ~15 seconds or on last word
                        if ts - seg_start > 15.0 or i == len(timeline) - 1:
                            segments.append({
                                "start": seg_start,
                                "end": ts + 0.5, # +0.5s minimum duration for final word
                                "text": " ".join(seg_text_words)
                            })
                            seg_start = ts
                            seg_text_words = []
                except Exception as e:
                    logger.warning(f"Error reading Storyteller chapter {chapter_index}: {e}")
                    
            if segments:
                rebuilt_segments = self.polisher.rebuild_fragmented_sentences(segments, ebook_text)
                alignment_map, align_method = self._generate_alignment_map_with_method(rebuilt_segments, ebook_text)
                if alignment_map:
                    self._save_alignment(abs_id, alignment_map, align_method)
                    logger.info(f"AlignmentService: Anchored Storyteller map stored for {abs_id} ({len(alignment_map)} points)")
                    return True
            
            logger.warning(f"AlignmentService: Anchored alignment failed for {abs_id}, falling back to unanchored map")

        # Fallback to unanchored map
        if ebook_text:
            clean_map = [
                {"char": 0, "ts": 0.0},
                {"char": len(ebook_text), "ts": storyteller_transcript.get_duration()},
            ]
            self._save_alignment(abs_id, clean_map, "storyteller_linear")
            logger.info(f"AlignmentService: Linear fallback map stored for {abs_id} ({len(clean_map)} points)")
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

        self._save_alignment(abs_id, clean_map, "storyteller")
        logger.info(f"AlignmentService: Unanchored Storyteller map stored for {abs_id} ({len(clean_map)} points)")
        return True

    def get_time_for_text(self, abs_id: str, query_text: str, char_offset_hint: int = None) -> Optional[float]:
        """
        Precise time lookup.
        If char_offset_hint is provided (from ebook reader), use it directly with the map.
        Otherwise, fuzzy search the text to find offset, then use map.
        """
        # 1. Fetch Alignment Map
        alignment = self._get_alignment(abs_id)
        if not alignment:
            return None
        
        map_points = alignment
        
        # 2. Resolve offset
        target_offset = char_offset_hint
        
        if target_offset is None:
            # Note: For now, KOSync always provides an offset or we calculate it.
            return None

        # 3. Interpolate Timestamp
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
        return self._interpolate_char_for_time(alignment, timestamp)

    @classmethod
    def _interpolate_char_for_time(cls, map_points: List[Dict], timestamp: float) -> Optional[int]:
        """Interpolate the character offset for a timestamp within a loaded map."""
        if not map_points:
            return None

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

        max_char = self._point_char(alignment[-1])
        if max_char <= 0:
            return None

        char = self._interpolate_char_for_time(alignment, timestamp)
        if char is None:
            return None

        return max(0.0, min(char / max_char, 1.0))

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
        alignment_map, _method = self._generate_alignment_map_with_method(segments, full_text)
        return alignment_map

    def _generate_alignment_map_with_method(self, segments: List[Dict], full_text: str) -> Tuple[List[Dict], str]:
        """
        Core Anchored Alignment Algorithm (Two-Pass), returning (map, method).
        Pass 1: High confidence (N=12) global search.
        Pass 2: Backfill start gap (N=6) if first anchor is late.
        method: 'lexical' (n-gram anchors), 'llm_anchor' (embedding rescue), or 'linear'.
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

        def _fallback(reason: str) -> Tuple[List[Dict], str]:
            # Embedding anchor rescue fires only here — when lexical anchoring fails.
            rescue = self._embedding_anchor_rescue(segments, full_text)
            if rescue:
                return rescue, "llm_anchor"
            return _build_linear_fallback_map(reason), "linear"

        # 1. Tokenize Transcript
        transcript_words = []
        for seg in segments:
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
        anchors = _find_anchors(transcript_words, book_words, n_size=12)
        
        # Sort by character position
        anchors.sort(key=lambda x: x['char'])
        
        # Filter Monotonic (Global) — Longest Increasing Subsequence
        valid_anchors = self._filter_monotonic_lis(anchors)
        logger.info(f"   📊 Monotonic LIS filter: {len(anchors)} candidates -> {len(valid_anchors)} valid")
        if len(anchors) > len(valid_anchors):
            logger.info(f"      📊 Dropped {len(anchors) - len(valid_anchors)} non-monotonic anchors")

        # 4. PASS 2: Backfill Start (N=6) "Work Backwards"
        # If the first anchor is significantly into the book, try to recover the intro.
        # Threshold: First anchor is > 1000 chars in AND > 30 seconds in
        if valid_anchors and valid_anchors[0]['char'] > 1000 and valid_anchors[0]['ts'] > 30.0:
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

        return final_map, "lexical"

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
        different content. Returns True (proceed) unless the embeddings show strong
        divergence. No-op (True) when disabled, no client, or embeddings unavailable."""
        if not self._env_true("OLLAMA_ALIGN_CONTENT_GUARD") or not self._ollama_ready():
            return True
        if not segments or not full_text:
            return True

        min_sim = self._env_float("OLLAMA_ALIGN_CONTENT_MIN_SIM", 0.45)
        transcript_text = " ".join((s.get("text") or "").strip() for s in segments).strip()
        t_samples = self._sample_passages(transcript_text)
        b_samples = self._sample_passages(full_text)
        if len(t_samples) < 1 or len(b_samples) < 1:
            return True

        vectors = self.ollama_client.embed(t_samples + b_samples)
        if not vectors or len(vectors) != len(t_samples) + len(b_samples):
            return True  # infra failure must not block alignment

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

    def _save_alignment(self, abs_id: str, alignment_map: List[Dict], align_method: str = None):
        """Upsert alignment to SQLite."""
        with self.database_service.get_session() as session:
            json_blob = json.dumps(alignment_map)

            # Check exist
            existing = session.query(BookAlignment).filter_by(abs_id=abs_id).first()
            if existing:
                existing.alignment_map_json = json_blob
                existing.align_method = align_method
                existing.last_updated = datetime.utcnow()
            else:
                new_align = BookAlignment(abs_id=abs_id, alignment_map_json=json_blob, align_method=align_method)
                session.add(new_align)
            
            # Context manager handles commit
            logger.info(f"   💾 Saved alignment for {abs_id} to DB.")

    def _get_alignment(self, abs_id: str) -> Optional[List[Dict]]:
        with self.database_service.get_session() as session:
            entry = session.query(BookAlignment).filter_by(abs_id=abs_id).first()
            if entry:
                return json.loads(entry.alignment_map_json)
            return None
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
