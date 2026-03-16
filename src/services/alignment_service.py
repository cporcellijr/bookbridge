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

try:
    from rapidfuzz.distance import Levenshtein
except Exception:
    Levenshtein = None

from src.db.models import BookAlignment
from src.utils.polisher import Polisher
from src.utils.logging_utils import time_execution

logger = logging.getLogger(__name__)

class AlignmentService:
    def __init__(self, database_service, polisher: Polisher):
        self.database_service = database_service
        self.polisher = polisher

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

        # 3. Anchored Alignment
        alignment_map = self._generate_alignment_map(rebuilt_segments, ebook_text)
        
        if not alignment_map:
            logger.error("   ❌ Failed to generate alignment map.")
            return False

        # 4. Store to Database
        self._save_alignment(abs_id, alignment_map)
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
                alignment_map = self._generate_alignment_map(rebuilt_segments, ebook_text)
                if alignment_map:
                    self._save_alignment(abs_id, alignment_map)
                    logger.info(f"AlignmentService: Anchored Storyteller map stored for {abs_id} ({len(alignment_map)} points)")
                    return True
            
            logger.warning(f"AlignmentService: Anchored alignment failed for {abs_id}, falling back to unanchored map")

        # Fallback to unanchored map
        if ebook_text:
            clean_map = [
                {"char": 0, "ts": 0.0},
                {"char": len(ebook_text), "ts": storyteller_transcript.get_duration()},
            ]
            self._save_alignment(abs_id, clean_map)
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

        self._save_alignment(abs_id, clean_map)
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
        # 1. Fetch Alignment Map
        alignment = self._get_alignment(abs_id)
        if not alignment:
            return None
        
        map_points = alignment
        target_ts = timestamp
        
        # 2. Binary search for interval
        left = 0
        right = len(map_points) - 1
        
        if target_ts <= map_points[0]['ts']:
            return self._point_char(map_points[0])
        if target_ts >= map_points[-1]['ts']:
            return self._point_char(map_points[-1])
            
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
            return self._point_char(p1)
            
        # 3. Interpolate
        time_span = p2['ts'] - p1['ts']
        p1_char = self._point_char(p1)
        p2_char = self._point_char(p2)
        char_span = p2_char - p1_char

        if time_span == 0: return p1_char

        ratio = (target_ts - p1['ts']) / time_span
        estimated_char = p1_char + (char_span * ratio)

        return int(estimated_char)

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

    def _generate_alignment_map_error_align(
        self,
        transcript_words: List[Dict],
        book_words: List[Dict],
        segments: List[Dict],
        full_text: str,
    ) -> Optional[List[Dict]]:
        """
        Build anchors via error-align word-level mapping.

        Returns a list of monotonic anchors (char/ts with token indices) or None when
        the dependency is unavailable or the output quality is too low.
        """
        try:
            from error_align import error_align
        except Exception as import_err:
            logger.warning("error-align unavailable; using legacy n-gram alignment (%s)", import_err)
            return None

        ref_words = [w.get("word", "") for w in book_words]
        hyp_words = [w.get("word", "") for w in transcript_words]
        if not ref_words or not hyp_words:
            return None

        try:
            alignments = error_align(ref=ref_words, hyp=hyp_words)
        except Exception as align_err:
            logger.warning("error-align failed; falling back to legacy n-gram alignment (%s)", align_err)
            return None

        anchors: List[Dict] = []
        b_idx = 0
        t_idx = 0

        def _norm_type(entry) -> str:
            raw = str(getattr(entry, "type", "")).lower()
            if "." in raw:
                raw = raw.split(".")[-1]
            return raw

        def _word_at(obj, key):
            val = getattr(obj, key, None)
            if isinstance(val, list) and val:
                return str(val[0])
            if val is None:
                return ""
            return str(val)

        def _advance(entry_type: str):
            nonlocal b_idx, t_idx
            # Conservative one-step advancement by operation type; if error-align returns
            # richer structures, sequential processing still produces dense anchors.
            if entry_type in {"match", "equal", "substitute", "replace", "similar"}:
                b_idx += 1
                t_idx += 1
            elif entry_type in {"insert", "insertion"}:
                t_idx += 1
            elif entry_type in {"delete", "deletion"}:
                b_idx += 1
            else:
                # Unknown op type; fail-safe forward motion to avoid stalling.
                b_idx += 1
                t_idx += 1

        for entry in alignments or []:
            if b_idx >= len(book_words) or t_idx >= len(transcript_words):
                break

            op = _norm_type(entry)
            if op in {"match", "equal"}:
                anchors.append(
                    {
                        "char": book_words[b_idx]["char"],
                        "ts": transcript_words[t_idx]["ts"],
                        "t_idx": transcript_words[t_idx]["orig_index"],
                        "b_idx": book_words[b_idx]["orig_index"],
                    }
                )
            elif op in {"substitute", "replace", "similar"}:
                ref_word = _word_at(entry, "ref") or book_words[b_idx]["word"]
                hyp_word = _word_at(entry, "hyp") or transcript_words[t_idx]["word"]
                # Keep minor substitutions only (apostrophe/plural/one-char drift).
                if self._word_edit_distance(ref_word, hyp_word) <= 1:
                    anchors.append(
                        {
                            "char": book_words[b_idx]["char"],
                            "ts": transcript_words[t_idx]["ts"],
                            "t_idx": transcript_words[t_idx]["orig_index"],
                            "b_idx": book_words[b_idx]["orig_index"],
                        }
                    )

            _advance(op)

        if not anchors:
            return None

        anchors.sort(key=lambda x: x["char"])
        valid = self._filter_monotonic_lis(anchors)
        if not valid:
            return None

        # Quality gates: avoid accepting tiny/low-coverage maps.
        # For longer books/transcripts, require a slightly denser anchor set.
        coverage = 0.0
        if len(full_text) > 0 and len(valid) > 1:
            coverage = max(0.0, (valid[-1]["char"] - valid[0]["char"]) / float(len(full_text)))
        min_anchor_count = max(5, min(20, len(transcript_words) // 300))
        transcript_anchor_ratio = (len(valid) / float(len(transcript_words))) if transcript_words else 0.0
        if len(valid) < min_anchor_count or coverage < 0.08 or transcript_anchor_ratio < 0.003:
            logger.info(
                "   ⚠️ error-align produced low-confidence anchors (%d anchors, %.1f%% coverage, %.2f%% token anchor ratio; min=%d, substitution_dist<=1)",
                len(valid),
                coverage * 100.0,
                transcript_anchor_ratio * 100.0,
                min_anchor_count,
            )
            return None

        logger.info(
            "   ✅ error-align anchors: %d candidates -> %d monotonic (%.1f%% coverage, %.2f%% token anchor ratio; substitution_dist<=1)",
            len(anchors),
            len(valid),
            coverage * 100.0,
            transcript_anchor_ratio * 100.0,
        )
        return valid

    @staticmethod
    def _word_edit_distance(left: str, right: str) -> int:
        """Tiny fallback for substitution filtering when rapidfuzz isn't installed."""
        if Levenshtein is not None:
            return int(Levenshtein.distance(left, right))

        a = left or ""
        b = right or ""
        if a == b:
            return 0
        # Small dynamic programming fallback; words are short so this is cheap.
        rows = len(a) + 1
        cols = len(b) + 1
        dp = [[0] * cols for _ in range(rows)]
        for i in range(rows):
            dp[i][0] = i
        for j in range(cols):
            dp[0][j] = j
        for i in range(1, rows):
            for j in range(1, cols):
                cost = 0 if a[i - 1] == b[j - 1] else 1
                dp[i][j] = min(
                    dp[i - 1][j] + 1,
                    dp[i][j - 1] + 1,
                    dp[i - 1][j - 1] + cost,
                )
        return dp[-1][-1]

    def _generate_alignment_map(self, segments: List[Dict], full_text: str) -> List[Dict]:
        """
        Core Anchored Alignment Algorithm (Two-Pass).
        Pass 1: High confidence (N=12) global search.
        Pass 2: Backfill start gap (N=6) if first anchor is late.
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
            return _build_linear_fallback_map("insufficient normalized tokens")

        # 3. Prefer error-align when available; fall back to legacy n-gram anchors.
        valid_anchors = self._generate_alignment_map_error_align(
            transcript_words=transcript_words,
            book_words=book_words,
            segments=segments,
            full_text=full_text,
        )

        # --- Helper for Legacy N-Gram Logic ---
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

        if not valid_anchors:
            logger.info("   ℹ️ error-align unavailable/insufficient; using legacy n-gram anchors")

            # PASS 1: Global Search (N=12)
            anchors = _find_anchors(transcript_words, book_words, n_size=12)

            # Sort by character position
            anchors.sort(key=lambda x: x['char'])

            # Filter Monotonic (Global) — Longest Increasing Subsequence
            valid_anchors = self._filter_monotonic_lis(anchors)
            logger.info(f"   📊 Monotonic LIS filter: {len(anchors)} candidates -> {len(valid_anchors)} valid")
            if len(anchors) > len(valid_anchors):
                logger.info(f"      📊 Dropped {len(anchors) - len(valid_anchors)} non-monotonic anchors")

            # PASS 2: Backfill Start (N=6) "Work Backwards"
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
            return _build_linear_fallback_map("no unique anchors found with N=12/N=6")

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

        return final_map

    def _save_alignment(self, abs_id: str, alignment_map: List[Dict]):
        """Upsert alignment to SQLite."""
        with self.database_service.get_session() as session:
            json_blob = json.dumps(alignment_map)
            
            # Check exist
            existing = session.query(BookAlignment).filter_by(abs_id=abs_id).first()
            if existing:
                existing.alignment_map_json = json_blob
                existing.last_updated = datetime.utcnow()
            else:
                new_align = BookAlignment(abs_id=abs_id, alignment_map_json=json_blob)
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


def _storyteller_filename_for_abs_chapter(chapter_index: int, prefix: str = "00000") -> str:
    """
    Build the bridge-managed canonical chapter filename for ABS chapter index N (0-based).

    This helper is for destination naming only (managed transcript store), not for
    source layout detection inside Storyteller asset folders.
    """
    return f"{prefix}-{chapter_index + 1:05d}.json"


def _resolve_storyteller_title_dir(assets_root: Path, abs_title: str) -> Optional[Path]:
    """
    Resolve the Storyteller title directory using exact match first,
    then normalized exact match (must be unique).
    """
    assets_dir = assets_root / "assets"
    if not assets_dir.exists() or not assets_dir.is_dir():
        return None

    exact_dir = assets_dir / abs_title
    if exact_dir.exists() and exact_dir.is_dir():
        return exact_dir

    target_key = _normalize_title_key(abs_title)
    if not target_key:
        return None

    candidates = []
    for child in assets_dir.iterdir():
        if child.is_dir() and _normalize_title_key(child.name) == target_key:
            candidates.append(child)

    if len(candidates) == 1:
        return candidates[0]

    if len(candidates) > 1:
        logger.warning(
            f"Storyteller assets match is ambiguous for '{_sanitize_log_data(abs_title)}' "
            f"({len(candidates)} directories)"
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
    transcriptions_dir: Path, expected_count: int
) -> tuple[bool, list[str], list[str]]:
    """
    Validate Storyteller chapter files by expected naming and exact count.
    Accept known source layouts:
      1) 00000-00001 ... 00000-N
      2) 00001-00001 ... 00001-N
      3) 00000-00001, 00001-00001 ... (N-1)-00001
      4) 00001-00001, 00002-00001 ... N-00001
    Returns (is_valid, source_filenames, destination_filenames).
    """
    if expected_count <= 0:
        logger.info(
            f"Storyteller validation failed at '{transcriptions_dir}': expected_count={expected_count} (must be > 0)"
        )
        return False, [], []

    expected_files = [_storyteller_filename_for_abs_chapter(i, "00000") for i in range(expected_count)]
    pattern = re.compile(r"^(\d{5})-(\d{5})\.json$")
    numeric_matches = []
    for p in transcriptions_dir.glob("*.json"):
        match = pattern.match(p.name)
        if match:
            numeric_matches.append((p.name, int(match.group(1)), int(match.group(2))))

    if len(numeric_matches) != expected_count:
        all_json = sorted([p.name for p in transcriptions_dir.glob("*.json")])
        numeric_names = sorted(name for name, _, _ in numeric_matches)
        first_slot_values = [first for _, first, _ in numeric_matches]
        second_slot_values = [second for _, _, second in numeric_matches]
        logger.info(
            "Storyteller validation failed at '%s': expected %d chapter files, found %d matching "
            "pattern '^\\d{5}-\\d{5}\\.json$' (total json=%d)",
            transcriptions_dir,
            expected_count,
            len(numeric_matches),
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
        if numeric_names:
            numeric_sample = ", ".join(numeric_names[:10])
            logger.info(
                "Storyteller validation numeric sample at '%s': %s%s",
                transcriptions_dir,
                numeric_sample,
                " ..." if len(numeric_names) > 10 else "",
            )
            logger.info(
                "Storyteller validation slot ranges at '%s': first_slot=%d..%d second_slot=%d..%d",
                transcriptions_dir,
                min(first_slot_values),
                max(first_slot_values),
                min(second_slot_values),
                max(second_slot_values),
            )
        return False, [], []

    candidate_layouts: list[tuple[str, list[str]]] = [
        (
            "prefix_00000",
            [f"00000-{i + 1:05d}.json" for i in range(expected_count)],
        ),
        (
            "prefix_00001",
            [f"00001-{i + 1:05d}.json" for i in range(expected_count)],
        ),
        (
            "chapter_first_zero_based",
            [f"{i:05d}-00001.json" for i in range(expected_count)],
        ),
        (
            "chapter_first_one_based",
            [f"{i + 1:05d}-00001.json" for i in range(expected_count)],
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
            return True, source_files, expected_files
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
        "Storyteller validation failed at '%s': no supported filename layout matched expected_count=%d",
        transcriptions_dir,
        expected_count,
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


def ingest_storyteller_transcripts(abs_id: str, abs_title: str, chapters: list) -> Optional[str]:
    """
    Copy Storyteller chapter JSON files into bridge-managed data storage and write a manifest.
    Returns manifest path on success.
    """
    assets_dir_raw = os.environ.get("STORYTELLER_ASSETS_DIR", "").strip()
    if not assets_dir_raw:
        return None

    chapter_list = chapters if isinstance(chapters, list) else []

    assets_root = Path(assets_dir_raw)
    title_dir = _resolve_storyteller_title_dir(assets_root, abs_title or "")
    if not title_dir:
        logger.info(f"Storyteller transcripts not found for '{abs_id}' (title='{_sanitize_log_data(abs_title)}')")
        return None

    transcriptions_dir = title_dir / "transcriptions"
    if not transcriptions_dir.exists() or not transcriptions_dir.is_dir():
        logger.info(f"Storyteller transcriptions directory missing for '{abs_id}' at '{transcriptions_dir}'")
        return None

    expected_count = len(chapter_list)
    chapterless_mode = expected_count <= 0
    if chapterless_mode:
        numeric_pattern = re.compile(r"^\d{5}-\d{5}\.json$")
        numeric_files = [p.name for p in transcriptions_dir.glob("*.json") if numeric_pattern.match(p.name)]
        expected_count = len(numeric_files)
        if expected_count <= 0:
            logger.info(
                f"Storyteller ingest skipped for '{abs_id}': no ABS chapters and no storyteller chapter files at "
                f"'{transcriptions_dir}'"
            )
            return None
        logger.info(
            f"Storyteller ingest chapterless mode for '{abs_id}': deriving {expected_count} chapters from "
            f"'{transcriptions_dir}'"
        )

    is_valid, source_files, expected_files = _validate_storyteller_chapters(transcriptions_dir, expected_count)
    if not is_valid:
        logger.info(
            f"Storyteller transcripts rejected for '{abs_id}': expected {expected_count} chapter files at "
            f"'{transcriptions_dir}'"
        )
        return None

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
    if chapterless_mode:
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
