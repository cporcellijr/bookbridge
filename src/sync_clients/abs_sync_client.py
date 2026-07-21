import json
import logging
import os
from typing import Optional

from src.api.api_clients import ABSClient
from src.db.models import Book, State
from src.sync_clients.sync_client_interface import SyncClient, SyncResult, UpdateProgressRequest, ServiceState
from src.utils.ebook_utils import EbookParser
from src.utils.progress_metadata import parse_service_timestamp
from src.utils.transcriber import AudioTranscriber
from pathlib import Path

logger = logging.getLogger(__name__)

class ABSSyncClient(SyncClient):
    def __init__(self, abs_client: ABSClient, transcriber: AudioTranscriber, ebook_parser: EbookParser, alignment_service=None):
        super().__init__(ebook_parser)
        self.abs_client = abs_client
        self.transcriber = transcriber
        self.alignment_service = alignment_service
        self.abs_progress_offset = float(os.getenv("ABS_PROGRESS_OFFSET_SECONDS", 0))
        self.delta_abs_thresh = float(os.getenv("SYNC_DELTA_ABS_SECONDS", 60))

    def is_configured(self) -> bool:
        return self.abs_client.is_configured()

    def check_connection(self):
        return self.abs_client.check_connection()

    def fetch_bulk_state(self):
        """Pre-fetch all ABS progress data at once."""
        return self.abs_client.get_all_progress_raw()

    def get_supported_sync_types(self) -> set:
        """ABS audiobook client only syncs audiobooks."""
        return {'audiobook'}

    def supports_book(self, book: Book) -> bool:
        return (getattr(book, "audio_source", None) or "ABS") == "ABS"

    def get_service_state(self, book: Book, prev_state: Optional[State], title_snip: str = "", bulk_context: dict = None) -> Optional[ServiceState]:
        abs_id = book.abs_id

        # Use bulk context if available, otherwise fetch individually
        if bulk_context and abs_id in bulk_context:
            item_data = bulk_context[abs_id]
            abs_ts = item_data.get('currentTime', 0)
            abs_last_update = item_data.get('lastUpdate')
            abs_finished = bool(item_data.get('isFinished'))
            # Note: Still need to convert to percentage using transcript
        else:
            response = self.abs_client.get_progress(abs_id)
            abs_ts = response.get('currentTime') if response is not None else None
            abs_last_update = response.get('lastUpdate') if response is not None else None
            abs_finished = bool(response.get('isFinished')) if response is not None else False

        if abs_ts is None:
            logger.info("🔍 ABS timestamp is None, probably not started the book yet")
            abs_ts = 0.0

        # ABS can mark an item finished without moving currentTime to the exact
        # duration. Treat the service's completion flag as authoritative so the
        # next cycle does not reinterpret a completed book as a small rewind.
        if abs_finished and book.duration and book.duration > 0:
            abs_ts = float(book.duration)
        abs_pct = 1.0 if abs_finished else self._abs_to_percentage(abs_ts, book)
        if abs_ts > 0 and abs_pct is None:
            # We lower this to debug to avoid spam if book is offline/unprocessed
            pass
        
        # Get previous ABS state values
        prev_abs_ts = prev_state.timestamp if prev_state else 0
        prev_abs_pct = prev_state.percentage if prev_state else 0
        
        delta = abs(abs_ts - prev_abs_ts) if abs_ts and prev_abs_ts else abs(abs_ts - prev_abs_ts) if abs_ts else 0

        current = {'pct': abs_pct, 'ts': abs_ts}
        # ABS mediaProgress.lastUpdate (epoch ms) — the service's own
        # "position last changed" signal.
        service_updated_at = parse_service_timestamp(abs_last_update)
        if service_updated_at is not None:
            current['service_updated_at'] = service_updated_at

        return ServiceState(
            current=current,
            previous_pct=prev_abs_pct,
            delta=delta,
            threshold=self.delta_abs_thresh,
            is_configured=True,
            display=("ABS", "{prev:.4%} -> {curr:.4%}"),
            value_seconds_formatter=lambda v: f"{v:.2f}s",
            value_formatter=lambda v: f"{v:.4%}"
        )

    def _abs_to_percentage(self, abs_seconds, book: Book):
        """Convert ABS timestamp to percentage using book duration (preferred) or transcript"""
        # 1. Try Book model duration (Golden Source)
        if book.duration and book.duration > 0:
            return min(max(abs_seconds / book.duration, 0.0), 1.0)
            
        # 2. Try Transcript file (Legacy fallback)
        transcript_path = book.transcript_file
        if not transcript_path:
            return None
            
        if transcript_path == "DB_MANAGED":
             if self.alignment_service:
                 dur = self.alignment_service.get_book_duration(book.abs_id)
                 if dur:
                     return min(max(abs_seconds / dur, 0.0), 1.0)
             return None

        try:
            # Check if file exists first
            if not os.path.exists(transcript_path):
                # If missing, we can't get duration from it.
                return None
                
            with open(transcript_path, 'r') as f:
                data = json.load(f)
                dur = data[-1]['end'] if isinstance(data, list) else data.get('duration', 0)
                return min(max(abs_seconds / dur, 0.0), 1.0) if dur > 0 else None
        except Exception as e:
            logger.debug(f"Failed to parse transcript for duration calculation: {e}")
            return None

    def get_text_from_current_state(self, book: Book, state: ServiceState) -> Optional[str]:
        abs_ts = state.current.get('ts')
        if not book or abs_ts is None:
            return None
            
        # Database-managed alignment
        if book.transcript_file == "DB_MANAGED" and self.alignment_service:
            # Inverse lookup: Time -> Char -> Text
            char_offset = self.alignment_service.get_char_for_time(book.abs_id, abs_ts)
            if char_offset is not None:
                 # Need book text
                 book_path = self.ebook_parser.resolve_book_path(book.ebook_filename)
                 if book_path and book_path.exists():
                     full_text, _ = self.ebook_parser.extract_text_and_map(book_path)
                     # Return context around char
                     start = max(0, char_offset - 50)
                     end = min(len(full_text), char_offset + 150)
                     return full_text[start:end]
            return None

        # Legacy File-Based
        # SMART FALLBACK: If file doesn't exist, try DB anyway (and self-heal)
        if hasattr(book, 'transcript_file') and book.transcript_file:
            path = Path(book.transcript_file)
            if not path.exists() and self.alignment_service:
                logger.warning(f"⚠️ '{book.abs_id}' Legacy transcript file missing: '{path}' — Attempting DB fallback")
                # Try DB lookup
                char_offset = self.alignment_service.get_char_for_time(book.abs_id, abs_ts)
                if char_offset is not None:
                     logger.info(f"✅ '{book.abs_id}' Found in DB despite missing file — Self-healing state")
                     # We can't easily save the book here without circular dependency or passing DB service
                     # But we can at least return valid text!
                     book_path = self.ebook_parser.resolve_book_path(book.ebook_filename)
                     if book_path and book_path.exists():
                         full_text, _ = self.ebook_parser.extract_text_and_map(book_path)
                         start = max(0, char_offset - 50)
                         end = min(len(full_text), char_offset + 150)
                         return full_text[start:end]

        return self.transcriber.get_text_at_time(book.transcript_file, abs_ts)

    def get_fallback_text(self, book: Book, state: ServiceState) -> Optional[str]:
        # Similar logic for fallback
        abs_ts = state.current.get('ts')
        if not book or abs_ts is None:
            return None
            
        if book.transcript_file == "DB_MANAGED" and self.alignment_service:
             # Just look a bit earlier?
             earlier_ts = max(0, abs_ts - 10)
             return self.get_text_from_current_state(book, ServiceState({'ts': earlier_ts}))

        return self.transcriber.get_previous_segment_text(book.transcript_file, abs_ts)

    def update_progress(self, book: Book, request: UpdateProgressRequest) -> SyncResult:
        book_title = book.abs_title or 'Unknown Book'
        if request.locator_result.percentage == 0.0:
            logger.info(f"🔄 '{book_title}' Locator percentage is 0.0% — Setting ABS progress to start of book")
            result, final_ts = self._update_abs_progress_with_offset(book.abs_id, 0.0)
            updated_state = {
                'ts': final_ts,
                'pct': 0.0
            }
            return SyncResult(final_ts, result.get("success", False), updated_state)

        # Route database-managed books to AlignmentService and legacy books to Transcriber.
        ts_for_text = None
        
        if book.transcript_file == "DB_MANAGED" and self.alignment_service:
            # Use database alignment.
            # We use the match_index (character offset) found by the EbookParser
            char_index = request.locator_result.match_index
            if char_index is not None:
                ts_for_text = self.alignment_service.get_time_for_text(
                    book.abs_id, 
                    request.txt, 
                    char_offset_hint=char_index
                )
            else:
                logger.debug(f"🔍 '{book_title}' Alignment lookup skipped: No character index provided in request")
                
        elif book.transcript_file and book.transcript_file != "DB_MANAGED":
            # Legacy Path: Use JSON File
            ts_for_text = self.transcriber.find_time_for_text(
                book.transcript_file, request.txt,
                hint_percentage=request.locator_result.percentage,
                char_offset=request.locator_result.match_index,
                book_title=book_title
            )
        if ts_for_text is not None:
            response = self.abs_client.get_progress(book.abs_id)
            abs_ts = response.get('currentTime') if response is not None else None
            if abs_ts is not None and ts_for_text < abs_ts:
                logger.info(f"🔄 '{book_title}' Not updating ABS progress — target timestamp {ts_for_text:.2f}s is before current ABS position {abs_ts:.2f}s")
                return SyncResult(abs_ts, True, {
                    'ts': abs_ts,
                    'pct': self._abs_to_percentage(abs_ts, book) or 0
                })

            prev_ts = abs_ts if abs_ts is not None else 0.0
            time_listened = (ts_for_text - prev_ts) if request.credit_listening else 0.0
            result, final_ts = self._update_abs_progress_with_offset(
                book.abs_id, ts_for_text, prev_ts, time_listened=time_listened
            )
            # Calculate percentage from timestamp for state
            pct = self._abs_to_percentage(final_ts, book)
            updated_state = {
                'ts': final_ts,
                'pct': pct or 0
            }
            return SyncResult(final_ts, result.get("success", False), updated_state)
        logger.warning(f"⚠️ '{book_title}' Not updating ABS progress — could not find timestamp for provided text")
        return SyncResult(None, False)

    def _update_abs_progress_with_offset(self, abs_id, ts, prev_abs_ts: float = 0, time_listened: float = 0):
        """Apply offset to timestamp and update ABS progress.

        Args:
            abs_id: ABS library item ID
            ts: New timestamp to set (seconds)
            prev_abs_ts: Previous ABS timestamp (kept for logging context)
            time_listened: Listening seconds to credit. Normally 0 (a bridge push
                from reading progress is not playback); a non-zero value is passed
                only when an audiobook-companion leader advanced the position and the
                forward audio delta should count as listening time.
        """
        adjusted_ts = max(round(ts + self.abs_progress_offset, 2), 0)
        if self.abs_progress_offset != 0:
            logger.debug(f"   📐 Adjusted timestamp: {ts}s → {adjusted_ts}s (offset: {self.abs_progress_offset:+.1f}s)")

        # Bridge pushes originate from reading progress (KoSync/Storyteller/Grimmory
        # leader), never from actual playback — send zero timeListened so reading
        # does not accrue listening time in ABS stats. Exception: an audiobook
        # companion (Storyteller) advancing the position is treated as listening, so
        # the caller credits the forward audio delta as time_listened.
        time_listened = max(0.0, round(float(time_listened or 0.0), 2))
        if time_listened > 0:
            logger.debug(f"   ⏱️ time_listened: {time_listened:.1f}s (listening push; prev: {prev_abs_ts:.1f}s → new: {adjusted_ts:.1f}s)")
        else:
            logger.debug(f"   ⏱️ time_listened: 0s (reading-driven push; prev: {prev_abs_ts:.1f}s → new: {adjusted_ts:.1f}s)")
        abs_ok = self.abs_client.update_progress(abs_id, adjusted_ts, time_listened)
        if isinstance(abs_ok, dict) and abs_ok.get("success"):
            try:
                from src.services.write_tracker import record_write
                record_write('ABS', abs_id)
            except ImportError:
                pass
        return abs_ok, adjusted_ts
