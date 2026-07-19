import os
import logging
from typing import Optional

from src.api.bookorbit_client import BookOrbitClient
from src.db.models import Book, State
from src.sync_clients.sync_client_interface import SyncClient, SyncResult, UpdateProgressRequest, ServiceState
from src.utils.ebook_utils import EbookParser
from src.utils.progress_metadata import parse_service_timestamp

logger = logging.getLogger(__name__)


class BookOrbitAudioSyncClient(SyncClient):
    """Audiobook sync client for BookOrbit.

    BookOrbit stores `positionSeconds` + `currentFileId` per book. Verified
    against the player source: positionSeconds is the seek position WITHIN the
    currentFileId track (the player does `goToFile(fileId, position)`), so a
    multi-file (track-per-chapter) audiobook needs the same track decomposition
    as Grimmory's folder-based audiobooks. Single-file books degenerate to
    within-file == absolute.
    """

    def __init__(self, bookorbit_client: BookOrbitClient, ebook_parser: EbookParser,
                 alignment_service=None, database_service=None, user_id: int = None):
        super().__init__(ebook_parser)
        self.client = bookorbit_client
        self.alignment_service = alignment_service
        self._database_service = database_service
        self._user_id = user_id
        self.delta_abs_thresh = float(os.getenv("SYNC_DELTA_ABS_SECONDS", 60))

    def is_configured(self) -> bool:
        return self.client.is_configured()

    def check_connection(self):
        return self.client.check_connection()

    def get_supported_sync_types(self) -> set:
        return {"audiobook"}

    def supports_book(self, book: Book) -> bool:
        if getattr(book, "audio_source", None) != "BookOrbit":
            return False
        if self._database_service is not None and self._user_id is not None:
            return bool(self._database_service.resolve_bookorbit_audio_id(self._user_id, book))
        return True

    @staticmethod
    def _coerce_id(value):
        if value is None or value == "":
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return value

    def _resolve_book_id(self, book: Book):
        """Resolve the BookOrbit audio book ID for this mapping.

        Prefers per-user UserBookOrbitLink audio_id, then shared legacy
        audio_provider_book_id / audio_source_id.
        """
        # Per-user resolution first
        if self._database_service is not None and self._user_id is not None:
            resolved = self._database_service.resolve_bookorbit_audio_id(self._user_id, book)
            if resolved:
                return self._coerce_id(resolved)
            return None
        # Legacy fallback: shared Book fields
        return self._coerce_id(
            getattr(book, "audio_provider_book_id", None)
            or getattr(book, "audio_source_id", None)
        )

    @staticmethod
    def _get_track_ranges(info: Optional[dict]) -> list[dict]:
        """Cumulative [start, end) ranges for each audio track, in play order."""
        ranges = []
        cursor = 0.0
        for track in (info or {}).get("tracks") or []:
            if not isinstance(track, dict):
                continue
            try:
                duration = float(track.get("duration_seconds") or 0.0)
            except (TypeError, ValueError):
                duration = 0.0
            ranges.append({
                "id": track.get("id"),
                "start": cursor,
                "duration": duration,
                "end": cursor + duration,
            })
            cursor += duration
        return ranges

    def _resolve_absolute_timestamp(
        self, info: Optional[dict], current_file_id, position_seconds
    ) -> Optional[float]:
        """Within-track position + currentFileId → absolute book timestamp."""
        if position_seconds is None:
            return None
        try:
            position = max(0.0, float(position_seconds))
        except (TypeError, ValueError):
            return None
        ranges = self._get_track_ranges(info)
        if len(ranges) <= 1:
            return position
        for r in ranges:
            if current_file_id is not None and r["id"] == current_file_id:
                return r["start"] + position
        # Multi-file book with an unknown/missing currentFileId — the within-track
        # position alone is ambiguous; let the caller fall back to percentage.
        return None

    def _resolve_resume_fields(self, info: Optional[dict], target_ts: float) -> dict:
        """Absolute book timestamp → {file_id, position_seconds} the player expects."""
        ranges = self._get_track_ranges(info)
        if not ranges:
            return {"file_id": (info or {}).get("primary_file_id"), "position_seconds": max(0.0, target_ts)}
        chosen = ranges[-1]
        for r in ranges:
            if target_ts < r["end"]:
                chosen = r
                break
        position = max(0.0, target_ts - chosen["start"])
        if chosen["duration"] > 0:
            position = min(position, chosen["duration"])
        return {
            "file_id": chosen["id"] or (info or {}).get("primary_file_id"),
            "position_seconds": position,
        }

    def _get_duration_seconds(self, book: Book, info: Optional[dict] = None) -> Optional[float]:
        for attr in ("audio_duration", "duration"):
            value = getattr(book, attr, None)
            try:
                if value is not None and float(value) > 0:
                    return float(value)
            except (TypeError, ValueError):
                continue
        if isinstance(info, dict):
            try:
                d = info.get("duration_seconds")
                if d is not None and float(d) > 0:
                    return float(d)
            except (TypeError, ValueError):
                pass
        return None

    def get_service_state(
        self,
        book: Book,
        prev_state: Optional[State],
        title_snip: str = "",
        bulk_context: dict = None,
    ) -> Optional[ServiceState]:
        book_id = self._resolve_book_id(book)
        if book_id is None:
            return None

        progress = self.client.get_audiobook_progress(book_id)
        if progress is None:
            return None

        current_pct = progress.get("pct")
        # Detail is cached in the client, so this does not add a request per poll.
        info = self.client.get_audiobook_info(book_id) or {}
        duration = self._get_duration_seconds(book, info)
        current_ts = self._resolve_absolute_timestamp(
            info, progress.get("current_file_id"), progress.get("position_seconds")
        )

        if current_pct is None and current_ts is not None and duration:
            current_pct = min(max(current_ts / duration, 0.0), 1.0)
        if current_pct is None:
            current_pct = 0.0
        if (current_ts is None or current_ts == 0.0) and current_pct and duration:
            current_ts = current_pct * duration

        prev_ts = prev_state.timestamp if prev_state and prev_state.timestamp is not None else 0.0
        prev_pct = prev_state.percentage if prev_state and prev_state.percentage is not None else 0.0
        delta = abs((current_ts or 0.0) - prev_ts)

        current = {"pct": current_pct, "ts": current_ts}
        service_updated_at = parse_service_timestamp(progress.get("updated_at"))
        if service_updated_at is not None:
            current["service_updated_at"] = service_updated_at

        return ServiceState(
            current=current,
            previous_pct=prev_pct,
            delta=delta,
            threshold=self.delta_abs_thresh,
            is_configured=self.client.is_configured(),
            display=("BookOrbitAudio", "{prev:.4%} -> {curr:.4%}"),
            value_seconds_formatter=lambda v: f"{v:.2f}s",
            value_formatter=lambda v: f"{v:.4%}",
        )

    def get_text_from_current_state(self, book: Book, state: ServiceState):
        return None

    def update_progress(self, book: Book, request: UpdateProgressRequest) -> SyncResult:
        book_id = self._resolve_book_id(book)
        if book_id is None:
            return SyncResult(None, False)

        info = self.client.get_audiobook_info(book_id) or {}
        target_ts = None
        if request.locator_result.percentage == 0.0:
            target_ts = 0.0
        elif book.transcript_file == "DB_MANAGED" and self.alignment_service and request.txt:
            target_ts = self.alignment_service.get_time_for_text(
                book.abs_id,
                request.txt,
                char_offset_hint=request.locator_result.match_index,
            )

        if target_ts is None:
            duration = self._get_duration_seconds(book, info)
            if duration:
                target_ts = max(0.0, min(duration, request.locator_result.percentage * duration))

        if target_ts is None:
            logger.warning(
                "BookOrbitAudio: cannot update '%s' — no target timestamp could be resolved",
                getattr(book, "abs_title", book.abs_id),
            )
            return SyncResult(None, False)

        percentage = request.locator_result.percentage
        resume = self._resolve_resume_fields(info, target_ts)
        success = self.client.update_audiobook_progress(
            book_id=book_id,
            position_seconds=resume["position_seconds"],
            percentage=percentage,
            current_file_id=resume["file_id"],
        )
        if success:
            try:
                from src.services.write_tracker import record_write
                record_write("BookOrbitAudio", book.abs_id, percentage)
            except ImportError:
                pass
        return SyncResult(target_ts, success, {"pct": percentage, "ts": target_ts})
