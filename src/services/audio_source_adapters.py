from __future__ import annotations

from dataclasses import dataclass
import logging
import math
import os
import re
import stat
from pathlib import Path
from typing import Optional

from src.api.api_clients import ABSClient
from src.services.current_audio_chapter import real_chapter_rows
from src.api.booklore_client import BookloreClient
from src.api.bookorbit_client import BookOrbitClient
from src.utils.config_loader import env_truthy
from src.utils.cache_paths import safe_cache_path
from src.utils.source_fingerprint import fingerprint
from src.utils.logging_utils import sanitize_log_data
from src.utils.series_metadata import extract_series_from_abs_metadata, extract_series_from_library_detail

logger = logging.getLogger(__name__)


@dataclass
class AudioResult:
    source: str
    source_id: str
    title: str
    subtitle: str = ""
    series_label: str = ""
    authors: str = ""
    language: str = ""
    cover_url: str = ""
    duration: Optional[float] = None
    display_name: str = ""
    provider_book_id: Optional[str] = None
    provider_file_id: Optional[str] = None
    path: str = ""
    series_name: Optional[str] = None
    series_sequence: Optional[float] = None
    series_source: Optional[str] = None


def _series_label(series_name: str | None, series_index: str | int | float | None, title: str) -> str:
    """Format a series label for display.

    When there is an index and the series name equals the book title
    (case-insensitive, ignoring surrounding whitespace), use "Book {index}"
    to avoid redundancy. When there is an index and the name differs, use
    "{seriesName} #{index}". When there is a name but no usable index, use
    the name alone. Otherwise return an empty string.
    """
    name = (series_name or "").strip()
    if not name:
        return ""
    idx: int | None = None
    if series_index is not None:
        try:
            if isinstance(series_index, float):
                if series_index.is_integer():
                    idx = int(series_index)
            elif isinstance(series_index, int):
                idx = series_index
            else:
                s = str(series_index).strip()
                if s.endswith(".0"):
                    s = s[:-2]
                idx = int(float(s))
        except (TypeError, ValueError):
            idx = None
    if idx is not None:
        if name.lower() == title.strip().lower():
            return f"Book {idx}"
        return f"{name} #{idx}"
    return name


class AudioSourceAdapter:
    source_name = ""

    def search(self, query: str) -> list[AudioResult]:
        raise NotImplementedError

    def get_metadata(self, source_id: str) -> Optional[dict]:
        raise NotImplementedError

    def get_cover_url(self, source_id: str) -> Optional[str]:
        raise NotImplementedError

    def get_audio_files(self, source_id: str, bridge_key: str | None = None) -> list[dict]:
        raise NotImplementedError

    def get_chapters(self, source_id: str) -> list[dict]:
        raise NotImplementedError


class ABSAudioSourceAdapter(AudioSourceAdapter):
    source_name = "ABS"

    def __init__(
        self,
        abs_client: ABSClient,
        data_dir: Path | None = None,
        catalog_items: list[dict] | None = None,
    ):
        self.abs_client = abs_client
        self.data_dir = Path(data_dir) if data_dir is not None else None
        self.catalog_items = catalog_items

    @staticmethod
    def _parse_library_scope() -> str | None:
        """
        Support both legacy and newer env shapes:
        - ABS_ONLY_SEARCH_IN_ABS_LIBRARY_ID=<library_id>
        - ABS_ONLY_SEARCH_IN_ABS_LIBRARY_ID=true + ABS_LIBRARY_ID=<library_id>
        """
        from src.utils.user_config import user_setting
        raw = (user_setting("ABS_ONLY_SEARCH_IN_ABS_LIBRARY_ID") or "").strip()
        if not raw:
            return None
        lowered = raw.lower()
        if lowered in {"false", "0", "off", "no", "none"}:
            return None
        if lowered in {"true", "1", "on", "yes"}:
            lib_id = (user_setting("ABS_LIBRARY_ID") or "").strip()
            return lib_id or None
        return raw

    def _get_search_pool(self) -> list[dict]:
        library_id = self._parse_library_scope()
        if self.catalog_items is not None:
            return [
                item for item in self.catalog_items
                if (not library_id or str(item.get("libraryId") or "") == library_id)
                and self.abs_client._item_has_audio(item)
            ]
        if library_id:
            return self.abs_client.get_audiobooks_for_lib(library_id)
        return self.abs_client.get_all_audiobooks()

    @staticmethod
    def _matches_query(item: dict, query: str) -> bool:
        if not query:
            return True
        media = item.get("media", {}) or {}
        metadata = media.get("metadata", {}) or item.get("metadata", {}) or {}
        haystack = " ".join(
            [
                str(metadata.get("title") or item.get("name") or ""),
                str(metadata.get("subtitle") or ""),
                str(metadata.get("authorName") or ""),
                str(metadata.get("seriesName") or ""),
            ]
        ).lower()
        return query.lower() in haystack

    @staticmethod
    def _get_title(item: dict) -> str:
        media = item.get("media", {}) or {}
        metadata = media.get("metadata", {}) or item.get("metadata", {}) or {}
        return metadata.get("title") or item.get("name") or "Unknown"

    @staticmethod
    def _get_authors(item: dict) -> str:
        media = item.get("media", {}) or {}
        metadata = media.get("metadata", {}) or item.get("metadata", {}) or {}
        return metadata.get("authorName") or ""

    @staticmethod
    def _extract_item_path(item: dict) -> str:
        media = item.get("media", {}) or {}
        metadata = media.get("metadata", {}) or item.get("metadata", {}) or {}
        candidates = [
            item.get("path"),
            item.get("folderPath"),
            item.get("relPath"),
            media.get("path"),
            media.get("folderPath"),
            media.get("relPath"),
            metadata.get("path"),
            metadata.get("folderPath"),
        ]

        for library_file in item.get("libraryFiles", []) or []:
            file_metadata = library_file.get("metadata", {}) or {}
            candidates.extend([
                library_file.get("path"),
                library_file.get("relPath"),
                file_metadata.get("path"),
                file_metadata.get("relPath"),
            ])

        for audio_file in media.get("audioFiles", []) or []:
            file_metadata = audio_file.get("metadata", {}) or {}
            candidates.extend([
                audio_file.get("path"),
                audio_file.get("relPath"),
                file_metadata.get("path"),
                file_metadata.get("relPath"),
            ])

        for candidate in candidates:
            text = str(candidate or "").strip()
            if text:
                return text
        return ""

    def search(self, query: str) -> list[AudioResult]:
        library_scope = self._parse_library_scope()
        query_mode = bool(query)
        pool = (
            self._get_search_pool()
            if self.catalog_items is not None or not query_mode
            else self.abs_client.search_audiobooks(query, library_id=library_scope)
        )
        results: list[AudioResult] = []
        self.catalog_incomplete = False
        for item in pool:
            item_id = str(item.get("id") or "")
            if not item_id:
                continue
            media = item.get("media", {}) or {}
            metadata = media.get("metadata", {}) or {}
            title = self._get_title(item)
            if title == "Unknown" and self.catalog_items is not None:
                # Catalog mode makes no per-item detail lookups: skip it and report the catalog incomplete.
                logger.info("ABS catalog item '%s' has no title; skipped and catalog marked incomplete", item_id)
                self.catalog_incomplete = True
                continue
            if (self.catalog_items is not None or not query_mode) and not self._matches_query(item, query):
                continue
            if title == "Unknown":
                details = self.get_metadata(item_id) or {}
                if details:
                    item = details
                    media = item.get("media", {}) or {}
                    metadata = media.get("metadata", {}) or item.get("metadata", {}) or {}
                    title = self._get_title(item)
            authors = self._get_authors(item)
            subtitle = metadata.get("subtitle") or ""
            series_label = (metadata.get("seriesName") or "").strip()
            series_name, series_seq = extract_series_from_abs_metadata(metadata)
            cover_url = self.get_cover_url(item_id) or ""
            duration = media.get("duration")
            results.append(
                AudioResult(
                    source="ABS",
                    source_id=item_id,
                    title=title,
                    subtitle=subtitle,
                    series_label=series_label,
                    series_name=series_name,
                    series_sequence=series_seq,
                    series_source="abs" if series_name else None,
                    authors=authors,
                    language=str(metadata.get("language") or "").strip(),
                    cover_url=cover_url,
                    duration=float(duration) if duration is not None else None,
                    display_name=title,
                    provider_book_id=item_id,
                    path=self._extract_item_path(item),
                )
            )
        return results

    def get_metadata(self, source_id: str) -> Optional[dict]:
        return self.abs_client.get_item_details(source_id)

    def get_cover_url(self, source_id: str) -> Optional[str]:
        if not self.abs_client.is_configured():
            return None
        return f"/api/cover-proxy/{source_id}"

    def get_audio_files(self, source_id: str, bridge_key: str | None = None) -> list[dict]:
        files = [dict(audio_file) for audio_file in (self.abs_client.get_audio_files(source_id) or [])]
        if not files or self.data_dir is None or not env_truthy("CTC_ENABLED"):
            return files

        cache_key = str(bridge_key or source_id).replace(":", "_")
        source_cache_dir = self.data_dir / "audio_cache" / cache_key / "source_tracks"
        for idx, audio_file in enumerate(files):
            if audio_file.get("local_path"):
                continue
            ext = str(audio_file.get("ext") or "mp3").lower().lstrip(".")
            if not ext.isalnum():
                ext = "mp3"
            local_path = source_cache_dir / f"track_{idx:03d}.{ext}"
            if not local_path.exists() or local_path.stat().st_size == 0:
                source_cache_dir.mkdir(parents=True, exist_ok=True)
                if not self.abs_client.download_file(audio_file["stream_url"], local_path):
                    logger.warning(
                        "ABS track download failed for item_id=%s track_index=%s; using stream for transcription",
                        source_id, idx,
                    )
                    continue
            audio_file["local_path"] = str(local_path)
        return files

    def get_chapters(self, source_id: str) -> list[dict]:
        details = self.get_metadata(source_id) or {}
        return details.get("media", {}).get("chapters", []) or []


class BookLoreAudioSourceAdapter(AudioSourceAdapter):
    source_name = "BookLore"

    def __init__(self, booklore_client: BookloreClient, data_dir: Path, catalog_items: list[dict] | None = None):
        self.booklore_client = booklore_client
        self.data_dir = Path(data_dir)
        self.catalog_items = catalog_items

    @staticmethod
    def _format_authors(book: dict) -> str:
        authors = book.get("authors")
        if isinstance(authors, str):
            return authors
        if isinstance(authors, list):
            cleaned = [str(a).strip() for a in authors if a]
            return ", ".join(cleaned)
        return ""

    @staticmethod
    def _safe_ext(track: dict) -> str:
        allowed = {"mp3", "m4a", "m4b", "flac", "ogg", "opus", "aac", "wav"}

        raw_ext = str(track.get("extension") or track.get("ext") or "").lower().strip().lstrip(".")
        if raw_ext in allowed:
            return raw_ext

        file_name = str(track.get("fileName") or track.get("filename") or "").strip()
        if "." in file_name:
            from_name = file_name.rsplit(".", 1)[-1].lower().lstrip(".")
            if from_name in allowed:
                return from_name

        mime = str(track.get("mimeType") or track.get("mime") or "").lower()
        codec = str(track.get("codec") or "").lower()
        descriptor = f"{mime} {codec}"
        if any(token in descriptor for token in ("mp3", "mpeg")):
            return "mp3"
        if any(token in descriptor for token in ("mp4", "m4a", "m4b", "aac", "mp4a")):
            return "m4b"
        return "mp3"

    @staticmethod
    def _to_seconds(raw_value) -> float | None:
        if raw_value is None or raw_value == "":
            return None
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            return None
        if value < 0:
            return None
        return value

    @staticmethod
    def _normalize_time_value(raw_value: float | None, key_name: str | None, use_ms_scale: bool | None = None) -> float | None:
        if raw_value is None:
            return None
        if use_ms_scale is not None:
            return raw_value / 1000.0 if use_ms_scale else raw_value
        key = (key_name or "").lower()
        hinted_ms = ("ms" in key) or ("millis" in key)
        if not hinted_ms:
            return raw_value / 1000.0 if raw_value >= 100000 else raw_value
        # Some Grimmory builds expose ms-suffixed keys with second values.
        # Only scale down when values look like true milliseconds.
        return raw_value / 1000.0 if raw_value >= 100000 else raw_value

    @staticmethod
    def _infer_ms_scale(raw_values: list[float], hinted_ms: bool, total_hint_raw: float | None = None) -> bool:
        if not hinted_ms or not raw_values:
            return False
        max_raw = max(raw_values)
        if max_raw >= 100000:
            return True
        if total_hint_raw is not None and total_hint_raw >= 100000:
            if max_raw >= 10000:
                return True
            # Many short chapter values can still be milliseconds if total is large.
            if len(raw_values) >= 8 and (total_hint_raw / max(max_raw, 1.0)) > 20:
                return True
        return False

    def _extract_duration_seconds(self, info: dict) -> float | None:
        for key in ("durationMs", "duration_ms", "duration", "totalDurationMs", "totalDuration"):
            raw_value = self._to_seconds(info.get(key))
            if raw_value and raw_value > 0:
                seconds = self._normalize_time_value(raw_value, key)
                if seconds and seconds > 0:
                    return seconds
        return None

    @staticmethod
    def _extract_book_path(book: dict) -> str:
        info = book.get("audiobookInfo") or {}
        primary_file = book.get("primaryFile") or {}
        candidates = [
            info.get("filePath"),
            info.get("filepath"),
            info.get("path"),
            primary_file.get("filePath"),
            primary_file.get("filepath"),
            primary_file.get("path"),
            book.get("filePath"),
            book.get("filepath"),
            book.get("path"),
        ]

        for track in info.get("tracks", []) or []:
            candidates.extend([
                track.get("filePath"),
                track.get("filepath"),
                track.get("path"),
            ])

        for candidate in candidates:
            text = str(candidate or "").strip()
            if text:
                return text
        return ""

    def search(self, query: str) -> list[AudioResult]:
        results: list[AudioResult] = []
        include_info = bool(query and query.strip())
        books = self.catalog_items if self.catalog_items is not None else self.booklore_client.search_audiobooks(query, include_info=include_info)
        for book in books:
            if self.catalog_items is not None:
                if not self.booklore_client._book_supports_audiobook(book):
                    continue
                if query and query.casefold() not in str(book.get("title") or "").casefold():
                    continue
            book_id = book.get("id")
            if book_id is None:
                continue
            info = book.get("audiobookInfo") or {}
            duration = self._extract_duration_seconds(info)
            title = book.get("title") or book.get("fileName") or f"Grimmory {book_id}"
            provider_file_id = info.get("bookFileId") or book.get("bookFileId")
            subtitle = (book.get("subtitle") or "").strip()
            series_name, series_seq = extract_series_from_library_detail(book)
            legacy_series = book.get("seriesName") or book.get("series_name") or book.get("series")
            if isinstance(legacy_series, dict):
                legacy_series = legacy_series.get("name")
            legacy_index = (
                book.get("seriesNumber")
                or book.get("seriesIndex")
                or book.get("series_number")
            )
            series_label = _series_label(legacy_series, legacy_index, title)
            results.append(
                AudioResult(
                    source="BookLore",
                    source_id=str(book_id),
                    title=title,
                    subtitle=subtitle,
                    series_label=series_label,
                    series_name=series_name,
                    series_sequence=series_seq,
                    series_source="booklore" if series_name else None,
                    authors=self._format_authors(book),
                    language=str(book.get("language") or "").strip(),
                    cover_url=self.get_cover_url(str(book_id)) or "",
                    duration=duration,
                    display_name=title,
                    provider_book_id=str(book_id),
                    provider_file_id=str(provider_file_id) if provider_file_id is not None else None,
                    path=self._extract_book_path(book),
                )
            )
        return results

    def get_metadata(self, source_id: str) -> Optional[dict]:
        book_info = self.booklore_client.get_audiobook_info(source_id)
        if not book_info:
            return None
        progress = self.booklore_client.get_audiobook_progress(source_id)
        if progress:
            book_info["audiobookProgress"] = progress
        return book_info

    def get_cover_url(self, source_id: str) -> Optional[str]:
        return f"/api/booklore/audiobook-cover/{source_id}"

    def get_audio_files(self, source_id: str, bridge_key: str | None = None) -> list[dict]:
        info = self.booklore_client.get_audiobook_info(source_id) or {}
        tracks = info.get("tracks") or []
        track_mode = "tracks"
        if not tracks:
            # Some Grimmory payloads expose chapter markers but no per-file tracks.
            # In this shape, stream endpoint often serves a single full-book file.
            chapters = info.get("chapters") or []
            if chapters:
                track_mode = "chapter_markers_single_stream"
                fallback_ext = self._safe_ext(
                    {
                        "extension": info.get("extension"),
                        "mimeType": info.get("mimeType"),
                        "codec": info.get("codec"),
                    }
                )
                tracks = [
                    {
                        "index": 0,
                        "durationMs": info.get("durationMs"),
                        "codec": info.get("codec"),
                        "mimeType": info.get("mimeType") or info.get("contentType"),
                        "extension": fallback_ext,
                    }
                ]
        if not tracks:
            logger.warning(
                "Grimmory audio files unavailable: source_id=%s info_keys=%s",
                source_id,
                sorted(info.keys()) if isinstance(info, dict) else [],
            )
            return []
        logger.debug(
            "Grimmory audio files: source_id=%s mode=%s count=%s",
            source_id,
            track_mode,
            len(tracks),
        )

        cache_key = bridge_key or f"booklore-{source_id}"
        source_cache_dir = self.data_dir / "audio_cache" / str(cache_key) / "source_tracks"
        source_cache_dir.mkdir(parents=True, exist_ok=True)

        files = []
        for idx, track in enumerate(tracks):
            download_index = track.get("index")
            if not isinstance(download_index, int):
                download_index = idx
            ext = self._safe_ext(track)
            local_path = source_cache_dir / f"track_{idx:03d}.{ext}"
            if not local_path.exists() or local_path.stat().st_size == 0:
                ok = self.booklore_client.download_audiobook_track(source_id, download_index, local_path)
                if not ok:
                    raise RuntimeError(
                        f"Grimmory track download failed for book_id={source_id} track_index={download_index}"
                    )
            files.append(
                {
                    "local_path": str(local_path),
                    "ext": ext,
                    "track_index": download_index,
                    "duration_ms": track.get("durationMs"),
                }
            )
        return files

    def _parse_marker_chapters(self, info: dict, source_id: str) -> list[dict]:
        """Existing Grimmory marker parsing moved unchanged for alignment."""
        chapters = info.get("chapters") or []
        if chapters:
            raw_total_duration = None
            for key in ("durationMs", "duration_ms", "duration", "totalDurationMs", "totalDuration"):
                raw_total_duration = self._to_seconds(info.get(key))
                if raw_total_duration is not None:
                    break

            raw_rows = []
            for idx, chapter in enumerate(chapters):
                start_raw = None
                start_key = None
                for key in (
                    "startTimeMs", "start_time_ms", "startTimeMillis", "startMillis",
                    "startMs", "start_ms", "startTime", "start_time", "start",
                    "offsetMs", "offset_ms", "offset",
                ):
                    start_raw = self._to_seconds(chapter.get(key))
                    if start_raw is not None:
                        start_key = key
                        break

                end_raw = None
                end_key = None
                for key in (
                    "endTimeMs", "end_time_ms", "endTimeMillis", "endMillis",
                    "endMs", "end_ms", "endTime", "end_time", "end",
                ):
                    end_raw = self._to_seconds(chapter.get(key))
                    if end_raw is not None:
                        end_key = key
                        break

                duration_raw = None
                duration_key = None
                for key in (
                    "durationMs", "duration_ms", "durationTimeMs", "durationTime",
                    "lengthMs", "length_ms", "length", "duration",
                ):
                    duration_raw = self._to_seconds(chapter.get(key))
                    if duration_raw is not None:
                        duration_key = key
                        break

                raw_rows.append(
                    {
                        "id": idx,
                        "title": chapter.get("title") or f"Chapter {idx + 1}",
                        "start_raw": start_raw,
                        "start_key": start_key,
                        "end_raw": end_raw,
                        "end_key": end_key,
                        "duration_raw": duration_raw,
                        "duration_key": duration_key,
                    }
                )

            start_values_raw = [float(row["start_raw"]) for row in raw_rows if row["start_raw"] is not None]
            end_values_raw = [float(row["end_raw"]) for row in raw_rows if row["end_raw"] is not None]
            duration_values_raw = [float(row["duration_raw"]) for row in raw_rows if row["duration_raw"] is not None]
            start_has_ms = any(
                row["start_raw"] is not None and (("ms" in str(row["start_key"]).lower()) or ("millis" in str(row["start_key"]).lower()))
                for row in raw_rows
            )
            end_has_ms = any(
                row["end_raw"] is not None and (("ms" in str(row["end_key"]).lower()) or ("millis" in str(row["end_key"]).lower()))
                for row in raw_rows
            )
            duration_has_ms = any(
                row["duration_raw"] is not None and (("ms" in str(row["duration_key"]).lower()) or ("millis" in str(row["duration_key"]).lower()))
                for row in raw_rows
            )

            use_ms_start = self._infer_ms_scale(start_values_raw, start_has_ms, raw_total_duration)
            use_ms_end = self._infer_ms_scale(end_values_raw, end_has_ms, raw_total_duration)
            use_ms_duration = self._infer_ms_scale(duration_values_raw, duration_has_ms, raw_total_duration)

            logger.debug(
                "Grimmory audio chapter unit inference: source_id=%s start_ms=%s end_ms=%s duration_ms=%s raw_total=%s",
                source_id,
                use_ms_start,
                use_ms_end,
                use_ms_duration,
                raw_total_duration,
            )

            converted_rows = []
            for row in raw_rows:
                start = self._normalize_time_value(row["start_raw"], row["start_key"], use_ms_start)
                end = self._normalize_time_value(row["end_raw"], row["end_key"], use_ms_end)
                duration = self._normalize_time_value(row["duration_raw"], row["duration_key"], use_ms_duration)
                converted_rows.append(
                    {
                        "id": row["id"],
                        "title": row["title"],
                        "start": float(start) if start is not None else None,
                        "end": float(end) if end is not None else None,
                        "duration": max(0.0, float(duration or 0.0)),
                        "explicit_start": row["start_raw"] is not None,
                        "explicit_end": row["end_raw"] is not None,
                    }
                )

            # Decide whether chapter rows are absolute ranges or duration-only rows.
            # Some Grimmory payloads expose per-chapter durations as end/duration fields.
            explicit_start_count = sum(1 for row in converted_rows if row["explicit_start"])
            end_values = [row["end"] for row in converted_rows if row["end"] is not None]
            ends_monotonic = (
                len(end_values) >= 2
                and all(end_values[i] >= end_values[i - 1] for i in range(1, len(end_values)))
            )
            explicit_starts = [row["start"] for row in converted_rows if row["explicit_start"] and row["start"] is not None]
            explicit_starts_all_zero = bool(explicit_starts) and all(abs(float(v)) < 0.001 for v in explicit_starts)

            mode = "absolute"
            if explicit_start_count == 0:
                mode = "duration_only"
                if ends_monotonic:
                    # If no explicit starts but ends are monotonic and look like timeline offsets,
                    # treat rows as absolute-end mode.
                    mode = "absolute_end_only"
            elif explicit_starts_all_zero and any(row["duration"] > 0 for row in converted_rows):
                # Guardrail for malformed payloads where every chapter "start" is reported as 0.
                mode = "duration_only"

            normalized = []
            cursor = 0.0
            for row in converted_rows:
                title = row["title"]
                if mode == "absolute":
                    start = row["start"] if row["start"] is not None else cursor
                    if row["end"] is not None and row["end"] > start:
                        end = row["end"]
                    else:
                        end = start + max(0.0, row["duration"])
                    if end <= start:
                        end = start
                    cursor = max(cursor, end)
                elif mode == "absolute_end_only":
                    start = cursor
                    end = row["end"] if row["end"] is not None else (start + max(0.0, row["duration"]))
                    if end < start:
                        end = start
                    cursor = end
                else:
                    # duration_only
                    start = cursor
                    duration = row["duration"]
                    if duration <= 0 and row["end"] is not None:
                        duration = max(0.0, row["end"])
                    end = start + duration
                    cursor = end

                normalized.append(
                    {
                        "id": row["id"],
                        "title": title,
                        "start": float(start),
                        "end": float(end),
                    }
                )

            valid = [c for c in normalized if c.get("end", 0) > c.get("start", 0)]
            if valid:
                logger.debug(
                    "Grimmory audio chapters: source_id=%s mode=%s count=%s end=%.1fs",
                    source_id,
                    mode,
                    len(valid),
                    float(valid[-1]["end"]),
                )
                return valid

        return []

    def get_real_chapters(self, source_id: str) -> list[dict]:
        """Only true Grimmory chapter rows, without fabricated titles or tracks."""
        info = self.booklore_client.get_audiobook_info(source_id) or {}
        raw = info.get("chapters") or []
        if not isinstance(raw, list):
            return []
        result = []
        start_keys = (
            "startTimeMs", "start_time_ms", "startTimeMillis", "startMillis",
            "startMs", "start_ms", "startTime", "start_time", "start",
            "offsetMs", "offset_ms", "offset",
        )
        for row in self._parse_marker_chapters(info, source_id):
            index = row["id"]
            if index >= len(raw) or not isinstance(raw[index], dict):
                continue
            marker = raw[index]
            # A start the parser had to infer (from ends or durations) is not a real marker.
            if not any(self._to_seconds(marker.get(key)) is not None for key in start_keys):
                continue
            result.append({
                "title": marker.get("title"),
                "start": row["start"], "end": row["end"],
            })
        return real_chapter_rows(result)

    def get_chapters(self, source_id: str) -> list[dict]:
        info = self.booklore_client.get_audiobook_info(source_id) or {}
        markers = self._parse_marker_chapters(info, source_id)
        if markers:
            return markers

        tracks = info.get("tracks") or []
        if tracks:
            synthetic = []
            cursor = 0.0
            for idx, track in enumerate(tracks):
                duration = None
                for key in ("durationMs", "duration_ms", "duration", "lengthMs", "length"):
                    duration = self._to_seconds(track.get(key))
                    if duration is not None:
                        if str(key).lower().endswith("ms"):
                            duration = duration / 1000.0
                        break
                duration = max(0.0, float(duration or 0.0))
                synthetic.append(
                    {
                        "id": idx,
                        "title": track.get("title") or f"Track {idx + 1}",
                        "start": cursor,
                        "end": cursor + duration,
                    }
                )
                cursor += duration
            logger.debug(
                "Grimmory audio chapters: source_id=%s mode=tracks count=%s end=%.1fs",
                source_id,
                len(synthetic),
                float(synthetic[-1]["end"]) if synthetic else 0.0,
            )
            return synthetic

        total_duration = self._extract_duration_seconds(info) or 0.0
        if total_duration <= 0:
            return []
        logger.debug(
            "Grimmory audio chapters: source_id=%s mode=duration_only count=1 end=%.1fs",
            source_id,
            float(total_duration),
        )
        return [{"id": 0, "title": "Audiobook", "start": 0.0, "end": total_duration}]


def _is_link_or_reparse_point(path: Path) -> bool:
    """True for symlinks and Windows junctions/reparse points (attribute absent on POSIX)."""
    if path.is_symlink():
        return True
    return bool(getattr(path.lstat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


class BookOrbitAudioSourceAdapter(AudioSourceAdapter):
    source_name = "BookOrbit"

    def __init__(self, bookorbit_client: BookOrbitClient, data_dir: Path):
        self.bookorbit_client = bookorbit_client
        self.data_dir = Path(data_dir)

    def search(self, query: str) -> list[AudioResult]:
        results: list[AudioResult] = []
        for book in self.bookorbit_client.search_audiobooks(query):
            book_id = book.get("id")
            if book_id is None:
                continue
            title = book.get("title") or f"BookOrbit {book_id}"
            duration = book.get("duration_seconds")
            subtitle = (book.get("subtitle") or "").strip()
            series_name, series_seq = extract_series_from_library_detail(book)
            series_label = _series_label(book.get("seriesName"), book.get("seriesIndex"), title)
            results.append(
                AudioResult(
                    source="BookOrbit",
                    source_id=str(book_id),
                    title=title,
                    subtitle=subtitle,
                    series_label=series_label,
                    series_name=series_name,
                    series_sequence=series_seq,
                    series_source="bookorbit" if series_name else None,
                    authors=book.get("authors") or "",
                    language=str(book.get("language") or "").strip(),
                    cover_url=self.get_cover_url(str(book_id)) or "",
                    duration=float(duration) if duration else None,
                    display_name=title,
                    provider_book_id=str(book_id),
                )
            )
        return results

    def get_metadata(self, source_id: str) -> Optional[dict]:
        book_info = self.bookorbit_client.get_audiobook_info(source_id)
        if not book_info:
            return None
        progress = self.bookorbit_client.get_audiobook_progress(source_id)
        if progress:
            book_info["audiobookProgress"] = progress
        return book_info

    def get_cover_url(self, source_id: str) -> Optional[str]:
        return f"/api/bookorbit/audiobook-cover/{source_id}"

    @staticmethod
    def _verified_mounted_track(track: dict) -> Path | None:
        """Resolve a provider-owned track on the approved mount, or fail closed.

        An absolutePath is only a candidate: BookOrbit may run in a different
        container namespace. Require matching provider identity, basename and
        size, and reject traversal and symlinked components before reuse.
        """
        raw = track.get("absolute_path")
        filename = track.get("filename")
        if track.get("id") is None or not isinstance(raw, str) or not raw:
            return None
        if (not isinstance(filename, str) or not filename
                or filename in (".", "..") or "/" in filename or "\\" in filename):
            return None
        try:
            expected_size = int(track.get("size_bytes"))
            if expected_size <= 0:
                return None
            root_text = os.environ.get("AUDIOBOOKS_DIR", "/audiobooks")
            root_path = Path(root_text)
            candidate = Path(raw)
            if not root_path.is_absolute() or not candidate.is_absolute() or ".." in candidate.parts:
                return None
            root = root_path.resolve(strict=True)
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)  # disallow paths outside the approved mount
            if candidate.name != filename or not resolved.is_file():
                return None
            # A symlink inside the mount can be retargeted after validation;
            # fall back to the downloader rather than trusting it.
            current = candidate
            while current != root_path and current != current.parent:
                if _is_link_or_reparse_point(current):
                    return None
                current = current.parent
            if resolved.stat().st_size != expected_size:
                return None
            with resolved.open("rb") as handle:
                if not handle.read(1):
                    return None
            return resolved
        except (OSError, ValueError, RuntimeError, TypeError):
            return None

    def get_audio_files(self, source_id: str, bridge_key: str | None = None) -> list[dict]:
        info = self.bookorbit_client.get_audiobook_info(source_id) or {}
        tracks = info.get("tracks") or []
        if not tracks:
            logger.warning(
                "BookOrbit audio files unavailable: source_id=%s info_keys=%s",
                source_id,
                sorted(info.keys()) if isinstance(info, dict) else [],
            )
            return []
        logger.debug("BookOrbit audio files: source_id=%s count=%s", source_id, len(tracks))

        # ':' in bridge keys ('bookorbit:42') is not a legal path char on Windows.
        cache_key = str(bridge_key or f"bookorbit-{source_id}").replace(":", "_")
        cache_root = self.data_dir / "audio_cache"
        # A bridge key comes from stored/external identifiers. Never let a
        # malformed key or a symlinked cache directory redirect downloads
        # (or failed-download cleanup) outside our generated-audio cache.
        raw_parent = cache_root / cache_key
        source_cache_dir = safe_cache_path(cache_root, cache_key)
        if (source_cache_dir is None
                or raw_parent.is_symlink()):
            raise ValueError("Unsafe BookOrbit audio cache directory")
        source_cache_dir = source_cache_dir / "source_tracks"
        if source_cache_dir.is_symlink():
            raise ValueError("Unsafe BookOrbit audio cache directory")
        try:
            source_cache_dir.resolve(strict=False).relative_to(
                cache_root.resolve(strict=False))
        except (OSError, ValueError) as exc:
            raise ValueError("Unsafe BookOrbit audio cache directory") from exc

        files = []
        for idx, track in enumerate(tracks):
            ext = str(track.get("format") or "mp3").lower().lstrip(".")
            if not re.fullmatch(r"[a-z0-9]{1,8}", ext):
                ext = "mp3"
            local_path = source_cache_dir / f"track_{idx:03d}.{ext}"
            try:
                expected_size = max(0, int(track.get("size_bytes") or 0))
            except (TypeError, ValueError):
                expected_size = 0
            mounted = self._verified_mounted_track(track)
            mounted_print = None
            if mounted is not None:
                try:
                    mounted_print = fingerprint(str(mounted))
                except OSError:
                    mounted = None
            if mounted is not None:
                duration = track.get("duration_seconds")
                files.append({
                    "local_path": str(mounted),
                    "ext": ext,
                    "track_index": idx,
                    "duration_ms": int(float(duration) * 1000) if duration else None,
                    "source_fingerprint": mounted_print,
                })
                continue
            cached_size = local_path.stat().st_size if local_path.exists() else 0
            if cached_size and expected_size and cached_size != expected_size:
                logger.warning(
                    "BookOrbit audio cache size mismatch: book_id=%s file_id=%s "
                    "cached=%s expected=%s; re-downloading",
                    source_id, track.get("id"), cached_size, expected_size,
                )
            if not cached_size or (expected_size and cached_size != expected_size):
                source_cache_dir.mkdir(parents=True, exist_ok=True)
                ok = self.bookorbit_client.download_file_to_path(track.get("id"), local_path)
                if not ok:
                    raise RuntimeError(
                        f"BookOrbit track download failed for book_id={source_id} file_id={track.get('id')}"
                    )
                downloaded_size = local_path.stat().st_size
                if not downloaded_size or (expected_size and downloaded_size != expected_size):
                    local_path.unlink()
                    raise RuntimeError(
                        f"Incomplete BookOrbit audio download for book_id={source_id} "
                        f"file_id={track.get('id')}: got {downloaded_size} bytes, "
                        f"expected {expected_size or 'non-empty file'}"
                    )
            duration = track.get("duration_seconds")
            files.append(
                {
                    "local_path": str(local_path),
                    "ext": ext,
                    "track_index": idx,
                    "duration_ms": int(float(duration) * 1000) if duration else None,
                }
            )
        return files

    def _parse_marker_chapters(self, info: dict, source_id: str, total_duration: float) -> list[dict]:
        """Existing BookOrbit marker parsing moved unchanged for alignment."""
        chapters = info.get("chapters") or []
        rows = []
        for chapter in chapters:
            if not isinstance(chapter, dict):
                continue
            try:
                start = float(chapter.get("startMs") or 0.0) / 1000.0
            except (TypeError, ValueError):
                continue
            rows.append({"title": chapter.get("title"), "start": start})
        if rows:
            normalized = []
            for idx, row in enumerate(rows):
                end = rows[idx + 1]["start"] if idx + 1 < len(rows) else max(total_duration, row["start"])
                normalized.append(
                    {
                        "id": idx,
                        "title": row["title"] or f"Chapter {idx + 1}",
                        "start": row["start"],
                        "end": float(end),
                    }
                )
            valid = [c for c in normalized if c["end"] > c["start"]]
            if valid:
                logger.debug(
                    "BookOrbit audio chapters: source_id=%s mode=markers count=%s end=%.1fs",
                    source_id, len(valid), valid[-1]["end"],
                )
                return valid

        return []

    def get_real_chapters(self, source_id: str) -> list[dict]:
        """Only true BookOrbit startMs markers; never track-based fallbacks."""
        info = self.bookorbit_client.get_audiobook_info(source_id) or {}
        try:
            total = float(info.get("duration_seconds") or 0.0)
        except (TypeError, ValueError, OverflowError):
            total = 0.0
        markers = []
        for marker in info.get("chapters") or []:
            if not isinstance(marker, dict) or marker.get("startMs") is None:
                continue
            try:
                start = float(marker["startMs"]) / 1000.0
            except (TypeError, ValueError, OverflowError):
                continue
            if math.isfinite(start) and start >= 0:
                markers.append({"title": marker.get("title"), "start": start})
        markers.sort(key=lambda row: row["start"])
        result = []
        for index, marker in enumerate(markers):
            end = markers[index + 1]["start"] if index + 1 < len(markers) else total
            result.append({**marker, "end": end})
        return real_chapter_rows(result)

    def get_chapters(self, source_id: str) -> list[dict]:
        """BookOrbit chapters are clean ``{title, startMs}`` markers (absolute,
        cumulative across tracks); ends are the next chapter's start. Falls back
        to per-track synthetic chapters, then a single whole-book chapter."""
        info = self.bookorbit_client.get_audiobook_info(source_id) or {}
        try:
            total_duration = float(info.get("duration_seconds") or 0.0)
        except (TypeError, ValueError):
            total_duration = 0.0

        markers = self._parse_marker_chapters(info, source_id, total_duration)
        if markers:
            return markers

        tracks = info.get("tracks") or []
        if tracks:
            synthetic = []
            cursor = 0.0
            for idx, track in enumerate(tracks):
                try:
                    duration = max(0.0, float(track.get("duration_seconds") or 0.0))
                except (TypeError, ValueError):
                    duration = 0.0
                title = Path(str(track.get("filename") or "")).stem or f"Track {idx + 1}"
                synthetic.append({"id": idx, "title": title, "start": cursor, "end": cursor + duration})
                cursor += duration
            logger.debug(
                "BookOrbit audio chapters: source_id=%s mode=tracks count=%s end=%.1fs",
                source_id, len(synthetic), synthetic[-1]["end"] if synthetic else 0.0,
            )
            return [c for c in synthetic if c["end"] > c["start"]]

        if total_duration <= 0:
            return []
        return [{"id": 0, "title": "Audiobook", "start": 0.0, "end": total_duration}]
