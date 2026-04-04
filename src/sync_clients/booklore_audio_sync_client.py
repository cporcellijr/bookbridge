import logging
import os
from typing import Optional

from src.api.booklore_client import BookloreClient
from src.db.models import Book, State
from src.sync_clients.sync_client_interface import SyncClient, SyncResult, UpdateProgressRequest, ServiceState
from src.utils.ebook_utils import EbookParser

logger = logging.getLogger(__name__)


class BookLoreAudioSyncClient(SyncClient):
    def __init__(self, booklore_client: BookloreClient, ebook_parser: EbookParser, alignment_service=None):
        super().__init__(ebook_parser)
        self.booklore_client = booklore_client
        self.alignment_service = alignment_service
        self.delta_abs_thresh = float(os.getenv("SYNC_DELTA_ABS_SECONDS", 60))

    def is_configured(self) -> bool:
        return self.booklore_client.is_configured()

    def check_connection(self):
        return self.booklore_client.check_connection()

    def get_supported_sync_types(self) -> set:
        return {"audiobook"}

    def supports_book(self, book: Book) -> bool:
        return getattr(book, "audio_source", None) == "BookLore"

    def _resolve_booklore_book_id(self, book: Book) -> Optional[str]:
        return (
            getattr(book, "audio_provider_book_id", None)
            or getattr(book, "audio_source_id", None)
        )

    def _resolve_booklore_file_id(self, book: Book, info: Optional[dict] = None) -> Optional[str]:
        file_id = getattr(book, "audio_provider_file_id", None)
        if file_id:
            return str(file_id)
        if not isinstance(info, dict):
            book_id = self._resolve_booklore_book_id(book)
            if not book_id:
                return None
            info = self.booklore_client.get_audiobook_info(book_id) or {}
        fetched = info.get("bookFileId")
        return str(fetched) if fetched is not None else None

    @staticmethod
    def _coerce_int(value) -> Optional[int]:
        if value is None or value == "":
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            try:
                return int(float(value))
            except (TypeError, ValueError):
                return None

    def _get_track_ranges(self, info: Optional[dict]) -> list[dict]:
        if not isinstance(info, dict):
            return []
        tracks = info.get("tracks")
        if not isinstance(tracks, list):
            return []

        ranges = []
        cursor_ms = 0
        for idx, track in enumerate(tracks):
            if not isinstance(track, dict):
                continue

            track_index = self._coerce_int(track.get("index"))
            if track_index is None:
                track_index = idx

            start_ms = self._coerce_int(track.get("cumulativeStartMs"))
            if start_ms is None:
                start_ms = cursor_ms

            duration_ms = self._coerce_int(track.get("durationMs"))
            if duration_ms is None or duration_ms < 0:
                continue

            end_ms = start_ms + duration_ms
            cursor_ms = max(cursor_ms, end_ms)
            ranges.append(
                {
                    "index": track_index,
                    "start_ms": max(start_ms, 0),
                    "duration_ms": max(duration_ms, 0),
                    "end_ms": max(end_ms, start_ms),
                }
            )

        return ranges

    def _resolve_absolute_timestamp_from_progress(
        self,
        book_id: str,
        progress: dict,
        duration: Optional[float],
        info: Optional[dict] = None,
    ) -> Optional[float]:
        current_pct = progress.get("pct")
        position_ms = self._coerce_int(progress.get("position_ms"))
        track_index = self._coerce_int(progress.get("track_index"))

        if position_ms is None:
            if current_pct is not None and duration is not None:
                logger.debug(
                    "GrimmoryAudio read fallback: book_id=%s mode=percentage_only pct=%.4f duration=%.2fs",
                    book_id,
                    float(current_pct),
                    float(duration),
                )
                return max(0.0, min(float(duration), float(current_pct) * float(duration)))
            return None

        if track_index is None:
            logger.debug(
                "GrimmoryAudio read resolved: book_id=%s mode=single_stream stored_position_ms=%s",
                book_id,
                position_ms,
            )
            return float(position_ms) / 1000.0

        track_ranges = self._get_track_ranges(info if info is not None else self.booklore_client.get_audiobook_info(book_id))
        for track in track_ranges:
            if track["index"] != track_index:
                continue
            absolute_ms = track["start_ms"] + max(position_ms, 0)
            logger.debug(
                "GrimmoryAudio read resolved: book_id=%s mode=track_reconstructed track_index=%s "
                "stored_position_ms=%s absolute_position_ms=%s",
                book_id,
                track_index,
                position_ms,
                absolute_ms,
            )
            return float(absolute_ms) / 1000.0

        if current_pct is not None and duration is not None:
            logger.debug(
                "GrimmoryAudio read fallback: book_id=%s mode=percentage_fallback track_index=%s "
                "stored_position_ms=%s pct=%.4f duration=%.2fs",
                book_id,
                track_index,
                position_ms,
                float(current_pct),
                float(duration),
            )
            return max(0.0, min(float(duration), float(current_pct) * float(duration)))

        logger.debug(
            "GrimmoryAudio read unresolved: book_id=%s mode=missing_track_metadata track_index=%s stored_position_ms=%s",
            book_id,
            track_index,
            position_ms,
        )
        return None

    def _resolve_resume_fields(
        self,
        book_id: str,
        target_ts: float,
        info: Optional[dict] = None,
    ) -> dict:
        info = info if isinstance(info, dict) else (self.booklore_client.get_audiobook_info(book_id) or {})
        absolute_target_ms = max(int(round(float(target_ts) * 1000.0)), 0)
        track_ranges = self._get_track_ranges(info)
        folder_based = bool(info.get("folderBased")) if isinstance(info, dict) else False

        if folder_based and track_ranges:
            chosen = track_ranges[-1]
            for track in track_ranges:
                if absolute_target_ms < track["end_ms"] or track is track_ranges[-1]:
                    chosen = track
                    break

            resume_position_ms = max(absolute_target_ms - chosen["start_ms"], 0)
            if chosen["duration_ms"] > 0:
                resume_position_ms = min(resume_position_ms, chosen["duration_ms"])

            return {
                "absolute_target_ms": absolute_target_ms,
                "position_ms": resume_position_ms,
                "track_index": chosen["index"],
                "track_position_ms": resume_position_ms,
                "mode": "folder_track",
            }

        return {
            "absolute_target_ms": absolute_target_ms,
            "position_ms": absolute_target_ms,
            "track_index": None,
            "track_position_ms": None,
            "mode": "single_stream",
        }

    def _get_duration_seconds(self, book: Book) -> Optional[float]:
        for attr in ("audio_duration", "duration"):
            value = getattr(book, attr, None)
            try:
                if value is not None and float(value) > 0:
                    return float(value)
            except (TypeError, ValueError):
                continue
        return None

    def get_service_state(
        self,
        book: Book,
        prev_state: Optional[State],
        title_snip: str = "",
        bulk_context: dict = None,
    ) -> Optional[ServiceState]:
        book_id = self._resolve_booklore_book_id(book)
        if not book_id:
            return None

        progress = self.booklore_client.get_audiobook_progress(book_id)
        if progress is None:
            return None

        current_pct = progress.get("pct")
        duration = self._get_duration_seconds(book)
        info = self.booklore_client.get_audiobook_info(book_id) or {}
        current_ts = self._resolve_absolute_timestamp_from_progress(book_id, progress, duration, info=info)
        if current_pct is None and current_ts is not None and duration:
            current_pct = min(max(current_ts / duration, 0.0), 1.0)
        if current_pct is None:
            current_pct = 0.0
        if current_ts is None and duration is not None:
            current_ts = current_pct * duration

        prev_ts = prev_state.timestamp if prev_state and prev_state.timestamp is not None else 0.0
        prev_pct = prev_state.percentage if prev_state and prev_state.percentage is not None else 0.0
        delta = abs((current_ts or 0.0) - prev_ts)

        return ServiceState(
            current={"pct": current_pct, "ts": current_ts},
            previous_pct=prev_pct,
            delta=delta,
            threshold=self.delta_abs_thresh,
            is_configured=self.booklore_client.is_configured(),
            display=("GrimmoryAudio", "{prev:.4%} -> {curr:.4%}"),
            value_seconds_formatter=lambda v: f"{v:.2f}s",
            value_formatter=lambda v: f"{v:.4%}",
        )

    def get_text_from_current_state(self, book: Book, state: ServiceState):
        return None

    def update_progress(self, book: Book, request: UpdateProgressRequest) -> SyncResult:
        book_id = self._resolve_booklore_book_id(book)
        if not book_id:
            return SyncResult(None, False)

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
            duration = self._get_duration_seconds(book)
            if duration:
                target_ts = max(0.0, min(duration, request.locator_result.percentage * duration))

        if target_ts is None:
            logger.warning(
                "GrimmoryAudio: cannot update '%s' because no target timestamp could be resolved",
                getattr(book, "abs_title", book.abs_id),
            )
            return SyncResult(None, False)

        percentage = request.locator_result.percentage
        info = self.booklore_client.get_audiobook_info(book_id) or {}
        resume_fields = self._resolve_resume_fields(book_id, target_ts, info=info)
        logger.debug(
            "GrimmoryAudio write resolved: book_id=%s mode=%s absolute_target_ms=%s "
            "resume_position_ms=%s track_index=%s",
            book_id,
            resume_fields["mode"],
            resume_fields["absolute_target_ms"],
            resume_fields["position_ms"],
            resume_fields["track_index"],
        )
        success = self.booklore_client.update_audiobook_progress(
            book_id=book_id,
            book_file_id=self._resolve_booklore_file_id(book, info=info),
            position_ms=resume_fields["position_ms"],
            percentage=percentage,
            track_index=resume_fields["track_index"],
            track_position_ms=resume_fields["track_position_ms"],
        )
        if success:
            try:
                from src.services.write_tracker import record_write

                record_write("BookLoreAudio", book.abs_id, percentage)
            except ImportError:
                pass
        return SyncResult(
            target_ts,
            success,
            {
                "pct": percentage,
                "ts": target_ts,
            },
        )
