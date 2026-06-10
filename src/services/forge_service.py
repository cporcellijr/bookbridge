import logging
import threading
import shutil
import tempfile
import time
import os
import html
import re
import uuid
from pathlib import Path
from urllib.parse import urljoin
import requests

from src.services.alignment_service import ingest_storyteller_transcripts
from src.utils.storyteller_transcript import StorytellerTranscript

logger = logging.getLogger(__name__)
AUDIO_EXTENSIONS = {'.mp3', '.m4b', '.m4a', '.flac', '.ogg', '.opus', '.wma', '.wav', '.aac'}


def _extract_series_from_abs_meta(metadata: dict, booklore_mode: bool = False) -> tuple:
    """Return (series_name, series_sequence) from ABS or BookLore metadata dict."""
    if not isinstance(metadata, dict):
        return None, None
    if booklore_mode:
        name = (metadata.get("seriesName") or "").strip() or None
        raw_seq = metadata.get("seriesNumber") or metadata.get("seriesSequence")
    else:
        series_list = metadata.get("series") or []
        if isinstance(series_list, list) and series_list:
            first = series_list[0]
            name = (first.get("name") if isinstance(first, dict) else str(first)).strip() or None
            raw_seq = first.get("sequence") if isinstance(first, dict) else None
        else:
            name = (metadata.get("seriesName") or "").strip() or None
            raw_seq = None
    sequence = None
    if raw_seq is not None:
        try:
            sequence = float(raw_seq)
        except (TypeError, ValueError):
            pass
    return name, sequence
DEFAULT_STAGE_MODE = "cleanup"
HARDLINK_STAGE_MODE = "hardlink"
VALID_STAGE_MODES = {DEFAULT_STAGE_MODE, HARDLINK_STAGE_MODE}

class ForgeService:
    def __init__(
        self,
        database_service,
        abs_client,
        booklore_client,
        storyteller_client,
        library_service,
        ebook_parser,
        transcriber,
        alignment_service,
        bookorbit_client=None,
    ):
        self.database_service = database_service
        self.abs_client = abs_client
        self.booklore_client = booklore_client
        self.storyteller_client = storyteller_client
        self.library_service = library_service
        self.ebook_parser = ebook_parser
        self.transcriber = transcriber
        self.alignment_service = alignment_service
        self.bookorbit_client = bookorbit_client
        self.active_tasks = set()
        self.lock = threading.Lock()
        
        # Load environment variables
        self.ABS_API_TOKEN = os.environ.get("ABS_KEY")
        self.ABS_API_URL = os.environ.get("ABS_SERVER")
        self.ABS_AUDIO_ROOT = Path(os.environ.get("AUDIOBOOKS_DIR", "/audiobooks"))
        self.storyteller_cleanup_grace_seconds = self._safe_int_env("STORYTELLER_CLEANUP_GRACE_SECONDS", 120)
        self.storyteller_recovery_max_wait_seconds = self._safe_int_env("STORYTELLER_RECOVERY_MAX_WAIT_MINUTES", 360) * 60
        self.storyteller_recovery_poll_interval_seconds = max(
            30, self._safe_int_env("STORYTELLER_RECOVERY_POLL_INTERVAL_MINUTES", 2) * 60
        )

    @staticmethod
    def safe_folder_name(name: str) -> str:
        invalid = '<>:"/\\|?*'
        name = html.escape(str(name).strip())[:150]
        for c in invalid:
            name = name.replace(c, '_')
        return name.strip() or "Unknown"

    @staticmethod
    def _safe_int_env(name: str, default: int) -> int:
        raw = os.environ.get(name, str(default))
        try:
            return max(0, int(raw))
        except Exception:
            return default

    @staticmethod
    def _safe_resolve(path: Path) -> Path:
        try:
            return path.resolve()
        except Exception:
            return path

    @staticmethod
    def _normalize_stage_mode(stage_mode: str) -> str:
        normalized = str(stage_mode or DEFAULT_STAGE_MODE).strip().lower()
        if normalized not in VALID_STAGE_MODES:
            return DEFAULT_STAGE_MODE
        return normalized

    @staticmethod
    def _normalize_text_source(source: str) -> str:
        normalized = str(source or "").strip()
        if not normalized:
            return ""

        source_map = {
            "grimmory": "Booklore",
            "booklore": "Booklore",
            "bookorbit": "BookOrbit",
            "abs": "ABS",
            "cwa": "CWA",
            "local file": "Local File",
        }
        return source_map.get(normalized.lower(), normalized)

    def _should_cleanup_staged_sources(self, stage_mode: str) -> bool:
        return self._normalize_stage_mode(stage_mode) == DEFAULT_STAGE_MODE

    def _stage_local_file(self, src_path: Path, dest_path: Path, stage_mode: str, context: str) -> str:
        src_path = Path(src_path)
        dest_path = Path(dest_path)
        normalized_mode = self._normalize_stage_mode(stage_mode)

        if not src_path.exists():
            raise FileNotFoundError(f"Source file not found: {src_path}")

        dest_path.parent.mkdir(parents=True, exist_ok=True)

        if dest_path.exists():
            try:
                if dest_path.samefile(src_path):
                    logger.debug(f"{context}: Staged file already in place: '{dest_path}'")
                    return "existing"
            except Exception:
                pass
            dest_path.unlink()

        if normalized_mode == HARDLINK_STAGE_MODE:
            try:
                os.link(src_path, dest_path)
                logger.info(f"{context}: Hardlinked local source '{src_path.name}'")
                return HARDLINK_STAGE_MODE
            except Exception as link_err:
                logger.warning(
                    f"{context}: Hardlink failed for '{src_path}' -> '{dest_path}' ({link_err}); "
                    "falling back to copy"
                )

        shutil.copy2(str(src_path), dest_path)
        logger.info(f"{context}: Copied local source '{src_path.name}'")
        return "copy"

    def _cleanup_staged_sources(
        self,
        course_dir: Path,
        staged_epub_path: Path = None,
        preserve_paths=None,
        context: str = "Forge"
    ) -> int:
        """
        Remove staged source audio/EPUB while preserving Storyteller output artifacts.
        """
        if not course_dir:
            return 0

        course_dir = Path(course_dir)
        if not course_dir.exists():
            return 0

        preserve_resolved = set()
        for path in preserve_paths or []:
            if not path:
                continue
            preserve_resolved.add(self._safe_resolve(Path(path)))

        deleted = 0
        failed = 0

        for file_path in course_dir.rglob("*"):
            if not file_path.is_file():
                continue
            if self._safe_resolve(file_path) in preserve_resolved:
                continue
            if file_path.suffix.lower() in AUDIO_EXTENSIONS:
                try:
                    file_path.unlink()
                    deleted += 1
                except Exception as cleanup_err:
                    failed += 1
                    logger.debug(f"{context}: Failed to delete source audio '{file_path}': {cleanup_err}")

        if staged_epub_path:
            staged_epub_path = Path(staged_epub_path)
            if (
                staged_epub_path.exists()
                and self._safe_resolve(staged_epub_path) not in preserve_resolved
            ):
                try:
                    staged_epub_path.unlink()
                    deleted += 1
                except Exception as cleanup_err:
                    failed += 1
                    logger.debug(f"{context}: Failed to delete staged epub '{staged_epub_path}': {cleanup_err}")

        logger.info(f"{context}: Cleanup complete - deleted {deleted} source file(s), failed {failed}.")
        return deleted

    @staticmethod
    def _extract_original_filename(text_item, fallback_filename=None):
        """
        Resolve the user's source EPUB filename from forge payload data.
        """
        if not isinstance(text_item, dict):
            return fallback_filename

        original_name = (
            text_item.get('original_ebook_filename')
            or text_item.get('ebook_filename')
            or text_item.get('filename')
        )
        return original_name or fallback_filename

    @staticmethod
    def _normalize_storyteller_title(value: str) -> str:
        """Normalize titles for exact identity matching without fuzzy fallback."""
        text = str(value or "").strip().lower()
        text = re.sub(r"[^\w\s]", " ", text)
        text = re.sub(r"_+", " ", text)
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    def _find_processed_epub(self, course_dir: Path):
        """
        Find Storyteller-produced EPUB artifacts in the staged course directory.
        Prefers latest modified candidate for robustness across naming variants.
        """
        candidates = {}
        try:
            for pattern in ("*readaloud*.epub", "*synced*.epub"):
                for p in course_dir.rglob(pattern):
                    if p.is_file():
                        candidates[str(p)] = p

            for p in course_dir.rglob("*.epub"):
                if p.is_file() and "synced" in str(p.parent).lower():
                    candidates[str(p)] = p
        except Exception:
            return None

        if not candidates:
            return None

        return sorted(
            candidates.values(),
            key=lambda x: x.stat().st_mtime if x.exists() else 0,
            reverse=True
        )[0]

    @staticmethod
    def _storyteller_link_ready(link_info) -> bool:
        """Return True when Storyteller reports a linked asset with a usable filepath."""
        if not isinstance(link_info, dict):
            return False

        filepath = str(link_info.get("filepath") or "").strip()
        missing = link_info.get("missing", 0)
        try:
            missing_flag = int(missing or 0) != 0
        except Exception:
            missing_flag = bool(missing)

        return bool(filepath) and not missing_flag

    def _get_storyteller_processing_state(self, st_client, book_uuid: str):
        """
        Return (details, ready, reason) for processing trigger readiness.

        A book is considered ready only when Storyteller exposes it via
        /api/v2/books/{uuid} and both the ebook and audiobook links are present.
        """
        if not book_uuid or not hasattr(st_client, "get_book_details"):
            return None, False, "details_unavailable"

        try:
            details = st_client.get_book_details(book_uuid)
        except Exception as details_err:
            logger.debug(f"Forge: get_book_details failed for {book_uuid}: {details_err}")
            return None, False, "details_error"

        if not isinstance(details, dict):
            return None, False, "not_visible"

        if not self._storyteller_link_ready(details.get("ebook")):
            return details, False, "ebook_unlinked"

        if not self._storyteller_link_ready(details.get("audiobook")):
            return details, False, "audiobook_unlinked"

        return details, True, "ready"

    @staticmethod
    def _normalize_storyteller_readaloud_state(details) -> dict:
        """Extract completion/error state from Storyteller book details."""
        readaloud = details.get("readaloud") if isinstance(details, dict) else None
        if not isinstance(readaloud, dict):
            readaloud = {}

        status = readaloud.get("status")
        aligned_at = details.get("alignedAt") if isinstance(details, dict) else None
        current_stage = readaloud.get("currentStage")
        stage_progress = readaloud.get("stageProgress")

        return {
            "status": status,
            "aligned": status == "ALIGNED" and bool(aligned_at),
            "errored": status == "ERROR",
            "aligned_at": aligned_at,
            "current_stage": current_stage,
            "stage_progress": stage_progress,
        }

    def _poll_auto_forge_completion(
        self,
        st_client,
        book_uuid: str,
        title: str,
        chapters: list,
        epub_cache: Path,
        processing_triggered: bool,
        poll_count: int,
    ):
        """
        Execute one completion-poll cycle for auto-forge.
        UUID is always known (pre-generated before TUS upload).
        """
        completion_method = None

        details, processing_ready, processing_state = self._get_storyteller_processing_state(
            st_client, book_uuid
        )
        readaloud_state = self._normalize_storyteller_readaloud_state(details)

        if not processing_triggered and processing_ready:
            try:
                st_client.trigger_processing(book_uuid)
                processing_triggered = True
            except Exception as trigger_err:
                logger.debug(f"Auto-Forge: trigger retry failed for {book_uuid}: {trigger_err}")
        elif not processing_triggered and poll_count % 4 == 0:
            logger.debug(
                f"Auto-Forge: delaying processing trigger for {book_uuid} "
                f"(Storyteller state={processing_state})"
            )

        if readaloud_state["errored"]:
            logger.warning(
                "Auto-Forge: Storyteller reported ERROR for %s (stage=%s progress=%s alignedAt=%s)",
                book_uuid,
                readaloud_state["current_stage"],
                readaloud_state["stage_progress"],
                readaloud_state["aligned_at"],
            )
        elif readaloud_state["aligned"]:
            completion_method = "storyteller_aligned"
        elif poll_count % 4 == 0:
            logger.info(
                "Auto-Forge: waiting for Storyteller alignment for %s "
                "(status=%s stage=%s progress=%s alignedAt=%s)",
                book_uuid,
                readaloud_state["status"],
                readaloud_state["current_stage"],
                readaloud_state["stage_progress"],
                readaloud_state["aligned_at"],
            )

        return {
            "processing_triggered": processing_triggered,
            "completion_method": completion_method,
            "readaloud_status": readaloud_state["status"],
            "aligned_at": readaloud_state["aligned_at"],
            "terminal_error": readaloud_state["errored"],
            "terminal_error_reason": "readaloud_error" if readaloud_state["errored"] else None,
            "storyteller_title": details.get("title") if isinstance(details, dict) else None,
        }

    def _copy_audio_files(self, abs_id: str, dest_folder: Path, stage_mode: str = DEFAULT_STAGE_MODE):
        """Copy audiobook files from ABS - Book Linker version"""
        headers = {"Authorization": f"Bearer {self.ABS_API_TOKEN}"}
        url = urljoin(self.ABS_API_URL, f"/api/items/{abs_id}")
        normalized_stage_mode = self._normalize_stage_mode(stage_mode)
        try:
            r = requests.get(url, headers=headers, timeout=15)
            r.raise_for_status()
            item = r.json()
            audio_files = item.get("media", {}).get("audioFiles", [])
            if not audio_files:
                logger.warning(f"⚠️ No audio files found for ABS '{abs_id}'")
                return False

            dest_folder.mkdir(parents=True, exist_ok=True)
            copied = 0

            for f in audio_files:
                meta = f.get("metadata", {})
                full_path = meta.get("path", "")
                filename = meta.get("filename", "")

                src_path = None
                # 1. Try exact path (rarely works across containers)
                if full_path and Path(full_path).exists():
                    src_path = Path(full_path)

                # 2. Smart Suffix Matching
                if not src_path and full_path:
                    parts = Path(full_path).parts
                    for i in range(4, 0, -1):
                        if len(parts) < i: continue
                        suffix = Path(*parts[-i:])
                        candidate = self.ABS_AUDIO_ROOT / suffix
                        if candidate.exists():
                            src_path = candidate
                            break

                # 3. Filename fallback
                if not src_path and filename:
                    matches = list(self.ABS_AUDIO_ROOT.glob(f"**/{filename}"))
                    if matches:
                        src_path = matches[0]

                if src_path and src_path.exists():
                    self._stage_local_file(
                        src_path=src_path,
                        dest_path=dest_folder / src_path.name,
                        stage_mode=normalized_stage_mode,
                        context="Forge audio",
                    )
                    copied += 1
                else:
                    # 4. API Download Fallback
                    logger.info(f"⚡ Local file not found, downloading via API: '{filename}'")
                    stream_url = f"{self.ABS_API_URL.rstrip('/')}/api/items/{abs_id}/file/{f.get('ino')}?token={self.ABS_API_TOKEN}"
                    dest_path = dest_folder / filename
                    # Use the ABS Client
                    if self.abs_client.download_file(stream_url, dest_path):
                        copied += 1
                    else:
                        logger.error(f"❌ Could not find or download audio file: '{filename}'")
            
            if copied == len(audio_files):
                return True
            else:
                logger.error(f"❌ Forge Strict Check Failed: Expected {len(audio_files)} files, copied {copied} — Aborting")
                return False
        except Exception as e:
            logger.error(f"❌ Failed to copy ABS '{abs_id}': {e}", exc_info=True)
            return False

    def _iter_booklore_audio_candidates(self, book_detail) -> list[dict]:
        if not isinstance(book_detail, dict):
            return []

        candidates = []
        for entry in [book_detail.get("primaryFile")]:
            if isinstance(entry, dict):
                candidates.append(entry)

        for key in ("bookFiles", "alternativeFormats", "supplementaryFiles"):
            entries = book_detail.get(key) or []
            if isinstance(entries, list):
                candidates.extend(entry for entry in entries if isinstance(entry, dict))

        audio_candidates = []
        for candidate in candidates:
            file_name = str(candidate.get("fileName") or candidate.get("filename") or "").strip()
            file_path = str(candidate.get("filePath") or candidate.get("filepath") or "").strip()
            suffix = Path(file_name or file_path).suffix.lower()
            book_type = str(candidate.get("bookType") or "").upper()
            if book_type != "AUDIOBOOK" and suffix not in AUDIO_EXTENSIONS:
                continue
            audio_candidates.append(
                {
                    **candidate,
                    "fileName": file_name,
                    "filePath": file_path,
                }
            )
        return audio_candidates

    def _resolve_booklore_local_path(self, file_info: dict) -> Path | None:
        if not isinstance(file_info, dict):
            return None

        raw_path = str(file_info.get("filePath") or file_info.get("filepath") or "").strip()
        file_name = str(file_info.get("fileName") or file_info.get("filename") or "").strip()

        if raw_path:
            candidate = Path(raw_path)
            if candidate.exists():
                return candidate

            parts = Path(raw_path).parts
            for i in range(4, 0, -1):
                if len(parts) < i:
                    continue
                suffix = Path(*parts[-i:])
                candidate = self.ABS_AUDIO_ROOT / suffix
                if candidate.exists():
                    return candidate

        if file_name:
            matches = list(self.ABS_AUDIO_ROOT.glob(f"**/{Path(file_name).name}"))
            if matches:
                return matches[0]

        return None

    def _resolve_booklore_local_audio_files(self, book_id: str, info: dict) -> list[dict]:
        book_detail = self.booklore_client.get_book_by_id(book_id)
        candidates = self._iter_booklore_audio_candidates(book_detail)
        resolved = []
        seen = set()

        for candidate in candidates:
            local_path = self._resolve_booklore_local_path(candidate)
            if not local_path:
                continue

            resolved_path = self._safe_resolve(local_path)
            if resolved_path in seen:
                continue
            seen.add(resolved_path)

            resolved.append(
                {
                    **candidate,
                    "local_path": local_path,
                    "resolved_name": Path(local_path).name,
                }
            )

        return resolved

    @staticmethod
    def _track_sort_key(idx: int, track: dict):
        raw_index = track.get("index")
        if isinstance(raw_index, int):
            return (raw_index, idx)
        return (idx, idx)

    def _stage_booklore_local_file(self, src_path: Path, dest_path: Path, stage_mode: str) -> str:
        result = self._stage_local_file(src_path, dest_path, stage_mode, "Grimmory audio")
        if result == HARDLINK_STAGE_MODE:
            logger.info("Grimmory audio: staged local file via hardlink '%s' -> '%s'", src_path.name, dest_path.name)
        elif result == "copy":
            logger.info("Grimmory audio: staged local file via copy '%s' -> '%s'", src_path.name, dest_path.name)
        else:
            logger.info("Grimmory audio: staged local file already present '%s'", dest_path.name)
        return result

    def _copy_booklore_audio_files(self, book_id: str, dest_folder: Path, stage_mode: str = DEFAULT_STAGE_MODE) -> bool:
        """Stage audiobook tracks from Grimmory into dest_folder."""
        def infer_ext(track: dict, info: dict) -> str:
            allowed = {"mp3", "m4a", "m4b", "flac", "ogg", "opus", "aac", "wav"}
            raw_ext = str(track.get("extension") or track.get("ext") or "").lower().strip().lstrip(".")
            if raw_ext in allowed:
                return raw_ext
            file_name = str(track.get("fileName") or "").strip()
            if "." in file_name:
                from_name = file_name.rsplit(".", 1)[-1].lower().lstrip(".")
                if from_name in allowed:
                    return from_name
            mime = str(track.get("mimeType") or info.get("mimeType") or info.get("contentType") or "").lower()
            codec = str(track.get("codec") or info.get("codec") or "").lower()
            descriptor = f"{mime} {codec}"
            if "mp3" in descriptor or "mpeg" in descriptor:
                return "mp3"
            if any(token in descriptor for token in ("mp4", "m4a", "m4b", "aac", "mp4a")):
                return "m4b"
            return "mp3"

        try:
            normalized_stage_mode = self._normalize_stage_mode(stage_mode)
            info = self.booklore_client.get_audiobook_info(book_id)
            if not info:
                logger.warning(f"No audiobook info found for Grimmory book '{book_id}'")
                return False

            logger.debug(f"Grimmory audiobook info keys for '{book_id}': {list(info.keys())}")
            tracks = info.get("tracks") or []
            track_mode = "tracks"
            if not tracks:
                chapters = info.get("chapters") or []
                if chapters:
                    # Chapter markers are not guaranteed to map 1:1 to stream indexes.
                    # Use a single-stream fallback when tracks are missing.
                    tracks = [
                        {
                            "index": 0,
                            "title": "Audiobook",
                            "codec": info.get("codec"),
                            "mimeType": info.get("mimeType") or info.get("contentType"),
                            "extension": infer_ext({}, info),
                        }
                    ]
                    track_mode = "chapter_markers_single_stream"
            if not tracks:
                logger.warning(
                    f"No audio tracks found for Grimmory book '{book_id}' "
                    f"(info keys: {list(info.keys())})"
                )
                return False
            logger.info(
                f"Grimmory audio mode for '{book_id}': {track_mode} ({len(tracks)} stream item(s))"
            )

            dest_folder.mkdir(parents=True, exist_ok=True)
            local_files = self._resolve_booklore_local_audio_files(book_id, info)
            book_detail = self.booklore_client.get_book_by_id(book_id, allow_refresh=False)
            local_candidate_count = len(self._iter_booklore_audio_candidates(book_detail))

            if local_files:
                should_use_single_stream_local = (
                    track_mode == "chapter_markers_single_stream"
                    or local_candidate_count == 1
                    or (len(local_files) == 1 and len(tracks) <= 1)
                )
                if should_use_single_stream_local:
                    local_file = local_files[0]
                    local_path = Path(local_file["local_path"])
                    ext = local_path.suffix.lstrip(".").lower() or infer_ext({}, info)
                    dest_path = dest_folder / f"track_000.{ext}"
                    logger.info("Grimmory audio: using single-stream local file '%s'", local_path.name)
                    self._stage_booklore_local_file(local_path, dest_path, normalized_stage_mode)
                    return True

                track_names_present = all(
                    str(track.get("fileName") or track.get("filename") or "").strip()
                    for track in tracks
                )
                if track_names_present:
                    by_name = {}
                    by_name_lower = {}
                    for local_file in local_files:
                        for name in {
                            str(local_file.get("fileName") or "").strip(),
                            str(local_file.get("resolved_name") or "").strip(),
                        }:
                            if not name:
                                continue
                            by_name.setdefault(name, local_file)
                            by_name_lower.setdefault(name.lower(), local_file)

                    matched_files = []
                    used_paths = set()
                    ordered_tracks = sorted(enumerate(tracks), key=lambda item: self._track_sort_key(item[0], item[1]))
                    for _, track in ordered_tracks:
                        track_name = str(track.get("fileName") or track.get("filename") or "").strip()
                        local_file = by_name.get(track_name) or by_name_lower.get(track_name.lower())
                        local_path = Path(local_file["local_path"]) if local_file else None
                        resolved_key = self._safe_resolve(local_path) if local_path else None
                        if not local_file or resolved_key in used_paths:
                            matched_files = []
                            break
                        used_paths.add(resolved_key)
                        matched_files.append((track, local_file))

                    if matched_files and len(matched_files) == len(tracks):
                        logger.info("Grimmory audio: using filename-based local track mapping")
                        for idx, (track, local_file) in enumerate(matched_files):
                            local_path = Path(local_file["local_path"])
                            ext = local_path.suffix.lstrip(".").lower() or infer_ext(track, info)
                            dest_path = dest_folder / f"track_{idx:03d}.{ext}"
                            self._stage_booklore_local_file(local_path, dest_path, normalized_stage_mode)
                        return True

                if len(local_files) == len(tracks):
                    logger.info("Grimmory audio: using positional track mapping")
                    ordered_tracks = [track for _, track in sorted(
                        enumerate(tracks), key=lambda item: self._track_sort_key(item[0], item[1])
                    )]
                    ordered_local_files = sorted(
                        local_files,
                        key=lambda item: str(
                            item.get("fileName") or item.get("resolved_name") or ""
                        ).lower(),
                    )
                    for idx, (track, local_file) in enumerate(zip(ordered_tracks, ordered_local_files)):
                        local_path = Path(local_file["local_path"])
                        ext = local_path.suffix.lstrip(".").lower() or infer_ext(track, info)
                        dest_path = dest_folder / f"track_{idx:03d}.{ext}"
                        self._stage_booklore_local_file(local_path, dest_path, normalized_stage_mode)
                    return True

                logger.info(
                    "Grimmory audio: local resolution incomplete, falling back to download "
                    "(resolved=%s expected=%s)",
                    len(local_files),
                    len(tracks),
                )

            downloaded = 0

            for idx, track in enumerate(tracks):
                ext = infer_ext(track, info)
                dest_path = dest_folder / f"track_{idx:03d}.{ext}"

                if track_mode == "chapter_markers_single_stream":
                    # For single M4B files with only chapter markers, the Grimmory
                    # /track/{index}/stream endpoint uses the M4B's container stream
                    # index, where stream 0 is often the cover art (mjpeg), not the
                    # audio. Download the whole file instead.
                    logger.info(
                        f"Grimmory audio (single-file): downloading whole file -> '{dest_path.name}'"
                    )
                    if self.booklore_client.download_book_to_path(
                        book_id, dest_path,
                        expected_size=int(info.get("totalSizeBytes") or 0),
                    ):
                        downloaded += 1
                    else:
                        logger.error(
                            f"Failed to download Grimmory whole-file audio for book '{book_id}'"
                        )
                else:
                    download_index = track.get("index") if isinstance(track.get("index"), int) else idx
                    logger.info(
                        f"Grimmory audio: downloading stream index {download_index} -> '{dest_path.name}'"
                    )
                    if self.booklore_client.download_audiobook_track(book_id, download_index, dest_path):
                        downloaded += 1
                    else:
                        logger.error(
                            f"Failed to download Grimmory track index {download_index} for book '{book_id}'"
                        )

            if downloaded == len(tracks):
                logger.info(f"Grimmory audio: downloaded all {downloaded} tracks for book '{book_id}'")
                return True
            else:
                logger.error(
                    f"Grimmory audio: expected {len(tracks)} tracks, downloaded {downloaded} — Aborting"
                )
                return False
        except Exception as e:
            logger.error(f"Failed to copy Grimmory audio for book '{book_id}': {e}", exc_info=True)
            return False

    @staticmethod
    def _collect_audio_files(root_dir: Path) -> list[Path]:
        root_dir = Path(root_dir)
        if not root_dir.exists():
            return []
        audio_files = []
        for candidate in sorted(root_dir.rglob("*")):
            if candidate.is_file() and candidate.suffix.lower() in AUDIO_EXTENSIONS:
                audio_files.append(candidate)
        return audio_files

    @staticmethod
    def _build_local_audio_inputs(audio_files: list[Path]) -> list[dict]:
        return [
            {
                "local_path": str(path),
                "ext": path.suffix.lstrip("."),
            }
            for path in audio_files
        ]

    def _get_whisper_audio_inputs(
        self,
        course_dir: Path,
        abs_id: str,
        audio_source: str | None,
        audio_source_id: str | None,
    ) -> list[dict]:
        staged_audio = self._collect_audio_files(course_dir)
        if staged_audio:
            logger.info("Auto-Forge: Whisper fallback using staged source audio")
            return self._build_local_audio_inputs(staged_audio)

        if audio_source == "BookLore" and audio_source_id:
            cache_root = self.ebook_parser.epub_cache_dir / "whisper_source_audio" / str(audio_source_id)
            cache_root.mkdir(parents=True, exist_ok=True)
            cached_audio = self._collect_audio_files(cache_root)
            if not cached_audio:
                if not self._copy_booklore_audio_files(audio_source_id, cache_root, stage_mode=DEFAULT_STAGE_MODE):
                    return []
                cached_audio = self._collect_audio_files(cache_root)
            if cached_audio:
                logger.info("Auto-Forge: Whisper fallback using Grimmory audio source")
                return self._build_local_audio_inputs(cached_audio)
            return []

        logger.info("Auto-Forge: Whisper fallback using ABS audio source")
        return self.abs_client.get_audio_files(abs_id) or []

    def start_manual_forge(
        self,
        abs_id,
        text_item,
        title,
        author,
        audio_source: str = None,
        audio_source_id: str = None,
        stage_mode: str = DEFAULT_STAGE_MODE,
    ):
        """
        Start manual forge process in background thread.
        """
        normalized_stage_mode = self._normalize_stage_mode(stage_mode)
        thread_kwargs = {}
        thread_options = {}
        if audio_source:
            thread_options["audio_source"] = audio_source
        if audio_source_id:
            thread_options["audio_source_id"] = audio_source_id
        if normalized_stage_mode != DEFAULT_STAGE_MODE:
            thread_options["stage_mode"] = normalized_stage_mode
        if thread_options:
            thread_kwargs["kwargs"] = thread_options
        thread = threading.Thread(
            target=self._forge_background_task,
            args=(abs_id, text_item, title, author),
            daemon=True,
            **thread_kwargs
        )
        thread.start()

    def _forge_background_task(
        self,
        abs_id,
        text_item,
        title,
        author,
        audio_source: str = None,
        audio_source_id: str = None,
        stage_mode: str = DEFAULT_STAGE_MODE,
    ):
        """
        Background thread: stage files locally, upload to Storyteller via TUS,
        trigger processing, extract alignment.
        """
        logger.info(f"🔨 Forge: Starting background task for '{title}'")
        stage_mode = self._normalize_stage_mode(stage_mode)
        logger.info(f"Forge: Staging mode '{stage_mode}'")

        with self.lock:
            self.active_tasks.add(title)

        temp_dir = None
        try:
            safe_title = self.safe_folder_name(title) if title else "Unknown"
            temp_dir = Path(tempfile.mkdtemp(prefix=".forge_tus_"))

            # Step 1: Copy audio files to temp staging dir
            if audio_source == "BookLore" and audio_source_id:
                audio_ok = self._copy_booklore_audio_files(audio_source_id, temp_dir, stage_mode=stage_mode)
            else:
                audio_ok = self._copy_audio_files(abs_id, temp_dir, stage_mode=stage_mode)
            if not audio_ok:
                logger.error(f"❌ Forge: Failed to copy audio files for '{abs_id}'")
                return
            logger.info(f"⚡ Forge: Audio files staged for '{title}'")

            # Step 2: Acquire text source (epub)
            epub_path = temp_dir / f"{safe_title}.epub"
            source = self._normalize_text_source(text_item.get('source', ''))
            text_success = False

            if source == 'Local File':
                src_path = Path(text_item.get('path', ''))
                if src_path.exists():
                    self._stage_local_file(src_path, epub_path, stage_mode, "Forge")
                    text_success = True
                    logger.info(f"⚡ Forge: Local epub copied: {src_path.name}")
                else:
                    logger.error(f"❌ Forge: Local file not found: '{src_path}'")
            elif source == 'Booklore':
                booklore_id = text_item.get('booklore_id')
                if booklore_id:
                    content = self.booklore_client.download_book(booklore_id)
                    if content:
                        epub_path.write_bytes(content)
                        text_success = True
                        logger.info(f"⚡ Forge: Grimmory epub downloaded")
                    else:
                        logger.error(f"❌ Forge: Grimmory download failed for '{booklore_id}'")
            elif source == 'BookOrbit':
                bookorbit_id = text_item.get('bookorbit_id') or text_item.get('source_id')
                if bookorbit_id and self.bookorbit_client:
                    content = self.bookorbit_client.download_book(bookorbit_id)
                    if content:
                        epub_path.write_bytes(content)
                        text_success = True
                        logger.info("⚡ Forge: BookOrbit epub downloaded")
                    else:
                        logger.error(f"❌ Forge: BookOrbit download failed for '{bookorbit_id}'")
                else:
                    logger.error(f"❌ Forge: BookOrbit client/id unavailable for '{bookorbit_id}'")
            elif source == 'ABS':
                abs_item_id = text_item.get('abs_id')
                if abs_item_id:
                    ebook_files = self.abs_client.get_ebook_files(abs_item_id)
                    if ebook_files:
                        stream_url = ebook_files[0].get('stream_url', '')
                        if stream_url and self.abs_client.download_file(stream_url, epub_path):
                            text_success = True
                            logger.info(f"⚡ Forge: ABS epub downloaded")
                        else:
                            logger.error(f"❌ Forge: ABS download failed for '{abs_item_id}'")
            elif source == 'CWA':
                download_url = text_item.get('download_url', '')
                cwa_id = text_item.get('cwa_id')
                cwa_client = self.library_service.cwa_client
                if download_url and cwa_client:
                    if cwa_client.download_ebook(download_url, epub_path):
                        text_success = True
                        logger.info(f"⚡ Forge: CWA epub downloaded")
                elif cwa_id and cwa_client:
                    book_info = cwa_client.get_book_by_id(cwa_id)
                    if book_info and book_info.get('download_url'):
                        if cwa_client.download_ebook(book_info['download_url'], epub_path):
                            text_success = True
                            logger.info(f"⚡ Forge: CWA epub downloaded via ID lookup")
                if not text_success:
                    logger.error(f"❌ Forge: CWA download failed")
            else:
                logger.error(f"❌ Forge: Unknown text source: '{source}'")

            if not text_success:
                logger.error(f"❌ Forge: Text acquisition failed — Aborting")
                return

            # Step 3: Upload to Storyteller via TUS
            st_client = self.storyteller_client
            book_uuid = str(uuid.uuid4())

            logger.info(f"Forge: Uploading epub to Storyteller ({book_uuid})...")
            if not st_client.upload_epub(str(epub_path), book_uuid):
                raise Exception("Failed to upload epub to Storyteller via TUS")

            audio_files = self._collect_audio_files(temp_dir)
            for audio_file in audio_files:
                logger.info(f"Forge: Uploading audio '{audio_file.name}' to Storyteller...")
                if not st_client.upload_audio_file(str(audio_file), book_uuid):
                    raise Exception(f"Failed to upload audio file '{audio_file.name}' to Storyteller via TUS")

            logger.info(f"⚡ Forge: All files uploaded to Storyteller ({book_uuid})")

            # Step 4: Wait for readiness and trigger processing
            ready = False
            ready_state = "not_visible"
            for ready_poll in range(120):
                _details, ready, ready_state = self._get_storyteller_processing_state(st_client, book_uuid)
                if ready:
                    break
                if ready_poll and ready_poll % 12 == 0:
                    logger.debug(f"Forge: Storyteller book {book_uuid} not ready yet (state={ready_state})")
                time.sleep(5)

            if ready:
                logger.info(f"⚡ Forge: Triggering processing for {book_uuid}...")
                st_client.trigger_processing(book_uuid)
            else:
                logger.warning(
                    f"⚠️ Forge: Storyteller book {book_uuid} never became API-ready (state={ready_state}); "
                    "processing may start automatically"
                )

            # Step 5: Wait for completion (poll for readaloud via API)
            MAX_WAIT = 3600
            POLL_INTERVAL = 30
            elapsed = 0
            completed = False

            logger.info(f"⚡ Forge: Waiting for processing to complete (polling every {POLL_INTERVAL}s, max {MAX_WAIT}s)")

            while elapsed < MAX_WAIT:
                time.sleep(POLL_INTERVAL)
                elapsed += POLL_INTERVAL

                try:
                    details = st_client.get_book_details(book_uuid)
                    if not isinstance(details, dict):
                        continue

                    readaloud_meta = details.get("readaloud", {})
                    readaloud_filepath = readaloud_meta.get("filepath") if isinstance(readaloud_meta, dict) else None
                    if not readaloud_filepath:
                        if elapsed % 120 == 0:
                            logger.debug(f"Forge: Still waiting for readaloud artifact ({book_uuid})")
                        continue

                    epub_cache = self.ebook_parser.epub_cache_dir
                    epub_cache.mkdir(parents=True, exist_ok=True)
                    completed_epub_path = epub_cache / f".forge_readaloud_{book_uuid}.epub"

                    if st_client.download_book(book_uuid, completed_epub_path, polling=True):
                        if completed_epub_path.exists() and completed_epub_path.stat().st_size > 0:
                            logger.info(f"⚡ Forge: Readaloud downloaded for {book_uuid}")

                            logger.info("⚡ Forge: Safety delay (60s) to allow Storyteller to finalize...")
                            time.sleep(60)

                            try:
                                logger.info("⚡ Forge: Extracting SMIL transcript from readaloud...")
                                chapters = []
                                if audio_source != "BookLore":
                                    item_details = self.abs_client.get_item_details(abs_id)
                                    chapters = item_details.get('media', {}).get('chapters', []) if item_details else []
                                book_text, _ = self.ebook_parser.extract_text_and_map(completed_epub_path)
                                raw_transcript = self.transcriber.transcribe_from_smil(
                                    abs_id, completed_epub_path, chapters, full_book_text=book_text
                                )
                                if not raw_transcript:
                                    logger.error(f"❌ Forge: SMIL extraction returned no transcript for '{abs_id}'")
                                else:
                                    success = self.alignment_service.align_and_store(abs_id, raw_transcript, book_text, chapters)
                                    if not success:
                                        logger.error(f"❌ Forge: align_and_store failed for '{abs_id}'")
                                    else:
                                        logger.info(f"✅ Forge: Alignment map stored for '{abs_id}'")
                            except Exception as e:
                                logger.error(f"❌ Forge: Alignment extraction failed: {e}")
                            finally:
                                try:
                                    completed_epub_path.unlink(missing_ok=True)
                                except Exception:
                                    pass

                            completed = True
                            break
                except Exception as e:
                    logger.warning(f"⚠️ Forge: Completion polling error: {e}")

            if not completed:
                logger.warning(f"⚠️ Forge: Processing timed out after {MAX_WAIT}s for '{title}'")

        except Exception as e:
            logger.error(f"❌ Forge: Background task failed for '{title}': {e}", exc_info=True)
        finally:
            if temp_dir and temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)
            with self.lock:
                self.active_tasks.discard(title)

    def start_auto_forge_match(self, abs_id, text_item, title, author, original_filename, original_hash,
                               audio_source: str = None, audio_source_id: str = None,
                               stage_mode: str = DEFAULT_STAGE_MODE):
        """
        Start Auto-Forge & Match pipeline in background thread.
        Links forged artifact to DB after completion.
        """
        normalized_stage_mode = self._normalize_stage_mode(stage_mode)
        thread_kwargs = {}
        if normalized_stage_mode != DEFAULT_STAGE_MODE:
            thread_kwargs["kwargs"] = {"stage_mode": normalized_stage_mode}
        thread = threading.Thread(
            target=self._auto_forge_background_task,
            args=(abs_id, text_item, title, author, original_filename, original_hash,
                  audio_source, audio_source_id),
            daemon=True,
            **thread_kwargs
        )
        thread.start()

    def _auto_forge_background_task(self, abs_id, text_item, title, author, original_filename, original_hash,
                                    audio_source: str = None, audio_source_id: str = None,
                                    stage_mode: str = DEFAULT_STAGE_MODE):
        """
        Background task for Auto-Forge & Match pipeline.
        Stage locally -> TUS upload -> Trigger -> Wait -> Download -> Align -> Update DB
        """
        logger.info(f"🔨 Auto-Forge: Starting pipeline for '{title}' (ABS {abs_id})")

        with self.lock:
            self.active_tasks.add(title)

        stage_mode = self._normalize_stage_mode(stage_mode)
        logger.info(f"Auto-Forge: Staging mode '{stage_mode}'")

        temp_dir = None

        try:
            original_ebook_filename = self._extract_original_filename(text_item, original_filename)
            safe_title = self.safe_folder_name(title) if title else "Unknown"
            temp_dir = Path(tempfile.mkdtemp(prefix=".forge_tus_"))

            # --- STAGE LOCALLY & UPLOAD VIA TUS ---

            # Copy Audio
            if audio_source == 'BookLore' and audio_source_id:
                if not self._copy_booklore_audio_files(audio_source_id, temp_dir, stage_mode=stage_mode):
                    raise Exception("Failed to copy Grimmory audio files")
            else:
                if not self._copy_audio_files(abs_id, temp_dir, stage_mode=stage_mode):
                    raise Exception("Failed to copy audio files")

            # Copy Text
            epub_path = temp_dir / f"{safe_title}.epub"
            source = self._normalize_text_source(text_item.get('source'))
            if source == 'Local File':
                self._stage_local_file(text_item.get('path'), epub_path, stage_mode, "Auto-Forge")
            elif source == 'Booklore':
                content = self.booklore_client.download_book(text_item.get('booklore_id'))
                if content: epub_path.write_bytes(content)
            elif source == 'BookOrbit':
                bookorbit_id = text_item.get('bookorbit_id') or text_item.get('source_id')
                content = self.bookorbit_client.download_book(bookorbit_id) if self.bookorbit_client and bookorbit_id else None
                if content:
                    epub_path.write_bytes(content)
                else:
                    logger.error(f"❌ Auto-Forge: BookOrbit download failed for '{bookorbit_id or 'unknown'}'")
            elif source == 'ABS':
                ebook_files = self.abs_client.get_ebook_files(text_item.get('abs_id'))
                if ebook_files: self.abs_client.download_file(ebook_files[0]['stream_url'], epub_path)
            elif source == 'CWA':
                cwa_client = getattr(self.library_service, 'cwa_client', None)
                download_url = text_item.get('download_url')
                cwa_id = text_item.get('cwa_id')
                text_downloaded = False
                if download_url and cwa_client:
                    text_downloaded = bool(cwa_client.download_ebook(download_url, epub_path))
                elif cwa_id and cwa_client:
                    book_info = cwa_client.get_book_by_id(cwa_id)
                    if book_info and book_info.get('download_url'):
                        text_downloaded = bool(cwa_client.download_ebook(book_info['download_url'], epub_path))
                if not text_downloaded:
                    logger.error(f"❌ Auto-Forge: CWA download failed for '{cwa_id or download_url or 'unknown'}'")
            else:
                raise Exception(f"Unknown or missing text source type: '{source}'")

            if not epub_path.exists():
                raise Exception("Failed to acquire text source")

            # Upload to Storyteller via TUS
            st_client = self.storyteller_client
            book_uuid = str(uuid.uuid4())

            logger.info(f"Auto-Forge: Uploading epub to Storyteller ({book_uuid})...")
            if not st_client.upload_epub(str(epub_path), book_uuid):
                raise Exception("Failed to upload epub to Storyteller via TUS")

            audio_files = self._collect_audio_files(temp_dir)
            for audio_file in audio_files:
                logger.info(f"Auto-Forge: Uploading audio '{audio_file.name}' to Storyteller...")
                if not st_client.upload_audio_file(str(audio_file), book_uuid):
                    raise Exception(f"Failed to upload audio file '{audio_file.name}' to Storyteller via TUS")

            logger.info(f"⚡ Auto-Forge: All files uploaded to Storyteller ({book_uuid})")

            # Wait for readiness and trigger processing
            item_details = None
            chapters = []
            try:
                item_details = self.abs_client.get_item_details(abs_id)
            except Exception as item_err:
                logger.debug(f"Auto-Forge: failed to fetch item details for chapters ({abs_id}): {item_err}")
            if item_details:
                chapters = item_details.get("media", {}).get("chapters", []) or []

            processing_triggered = False
            ready = False
            ready_state = "not_visible"
            for ready_poll in range(120):
                _ready_details, ready, ready_state = self._get_storyteller_processing_state(
                    st_client, book_uuid
                )
                if ready:
                    break
                if ready_poll and ready_poll % 12 == 0:
                    logger.debug(
                        f"Auto-Forge: Storyteller book {book_uuid} not ready yet (state={ready_state})"
                    )
                time.sleep(5)

            if ready:
                logger.info(f"Auto-Forge: Triggering processing for {book_uuid}")
                try:
                    st_client.trigger_processing(book_uuid)
                    processing_triggered = True
                except Exception as trigger_err:
                    logger.warning(f"Auto-Forge: Failed to trigger processing for {book_uuid}: {trigger_err}")
            else:
                logger.warning(
                    f"Auto-Forge: Storyteller book {book_uuid} never became API-ready (state={ready_state}); "
                    "continuing with recovery polling"
                )

            # --- WAIT FOR COMPLETION ---
            MAX_WAIT = 3600
            POLL_INTERVAL = 30
            elapsed = 0
            poll_count = 0
            completion_method = None
            last_readaloud_status = None
            last_aligned_at = None
            storyteller_title = None

            epub_cache = self.ebook_parser.epub_cache_dir
            if not epub_cache.exists():
                epub_cache.mkdir(parents=True, exist_ok=True)

            while elapsed < MAX_WAIT:
                time.sleep(POLL_INTERVAL)
                elapsed += POLL_INTERVAL
                poll_count += 1

                poll_result = self._poll_auto_forge_completion(
                    st_client=st_client,
                    book_uuid=book_uuid,
                    title=title,
                    chapters=chapters,
                    epub_cache=epub_cache,
                    processing_triggered=processing_triggered,
                    poll_count=poll_count,
                )
                processing_triggered = poll_result["processing_triggered"]
                completion_method = poll_result["completion_method"]
                last_readaloud_status = poll_result["readaloud_status"]
                last_aligned_at = poll_result["aligned_at"]
                storyteller_title = poll_result.get("storyteller_title") or storyteller_title
                if poll_result["terminal_error"]:
                    logger.warning(
                        "Auto-Forge: aborting because Storyteller reported %s for %s "
                        "(reason=%s alignedAt=%s)",
                        last_readaloud_status,
                        book_uuid,
                        poll_result["terminal_error_reason"],
                        last_aligned_at,
                    )
                    book = self.database_service.get_book(abs_id)
                    if book:
                        book.status = "error"
                        self.database_service.save_book(book)
                    return
                if completion_method:
                    break

            if not completion_method:
                timeout_reason = []
                if last_readaloud_status:
                    timeout_reason.append(f"readaloud_{last_readaloud_status}")
                else:
                    timeout_reason.append("readaloud_unknown")
                reason_str = ",".join(timeout_reason) if timeout_reason else "unknown"

                logger.warning(
                    f"Auto-Forge timeout: abs_id={abs_id} elapsed={elapsed}s polls={poll_count} reason={reason_str} "
                    f"book_uuid={book_uuid} alignedAt={last_aligned_at}"
                )
                book = self.database_service.get_book(abs_id)
                if book:
                    book.status = "forging"
                    self.database_service.save_book(book)
                logger.info(
                    f"Auto-Forge: entering extended recovery polling for {self.storyteller_recovery_max_wait_seconds}s "
                    f"(interval={self.storyteller_recovery_poll_interval_seconds}s)"
                )

                recovery_elapsed = 0
                while recovery_elapsed < self.storyteller_recovery_max_wait_seconds and not completion_method:
                    time.sleep(self.storyteller_recovery_poll_interval_seconds)
                    recovery_elapsed += self.storyteller_recovery_poll_interval_seconds
                    poll_count += 1

                    poll_result = self._poll_auto_forge_completion(
                        st_client=st_client,
                        book_uuid=book_uuid,
                        title=title,
                        chapters=chapters,
                        epub_cache=epub_cache,
                        processing_triggered=processing_triggered,
                        poll_count=poll_count,
                    )
                    processing_triggered = poll_result["processing_triggered"]
                    completion_method = poll_result["completion_method"]
                    last_readaloud_status = poll_result["readaloud_status"]
                    last_aligned_at = poll_result["aligned_at"]
                    storyteller_title = poll_result.get("storyteller_title") or storyteller_title
                    if poll_result["terminal_error"]:
                        logger.warning(
                            "Auto-Forge: aborting during recovery because Storyteller reported %s for %s "
                            "(reason=%s alignedAt=%s)",
                            last_readaloud_status,
                            book_uuid,
                            poll_result["terminal_error_reason"],
                            last_aligned_at,
                        )
                        book = self.database_service.get_book(abs_id)
                        if book:
                            book.status = "error"
                            self.database_service.save_book(book)
                        return

                if not completion_method:
                    logger.warning(
                        f"Auto-Forge: extended recovery timed out for abs_id={abs_id} "
                        f"elapsed={recovery_elapsed}s; keeping status='forging'"
                    )
                    return

            logger.info("Auto-Forge: Completion confirmed via %s", completion_method)

            # Grace wait before download to let Storyteller finish internal writes.
            if self.storyteller_cleanup_grace_seconds > 0:
                logger.info(
                    f"Auto-Forge: Grace wait before download: {self.storyteller_cleanup_grace_seconds}s"
                )
                time.sleep(self.storyteller_cleanup_grace_seconds)

            # --- DOWNLOAD ---
            no_epub_cache = os.environ.get("STORYTELLER_NO_EPUB_CACHE", "false").lower() == "true"
            target_filename = f"storyteller_{book_uuid}.epub"
            target_path = epub_cache / target_filename

            if no_epub_cache:
                original_name = Path(str(original_ebook_filename or original_filename or "")).name
                nocache_candidates = []
                if original_name:
                    nocache_candidates.append(self.ebook_parser.epub_cache_dir / original_name)
                source_path = text_item.get('path') if isinstance(text_item, dict) else None
                if source_path:
                    nocache_candidates.append(Path(source_path))
                try:
                    nocache_candidates.append(self.ebook_parser.resolve_book_path(original_name))
                except Exception:
                    pass

                resolved = None
                for c in nocache_candidates:
                    try:
                        if c and Path(c).exists():
                            resolved = Path(c)
                            break
                    except Exception:
                        continue

                if resolved:
                    logger.info(
                        "⚡ Auto-Forge: STORYTELLER_NO_EPUB_CACHE=true; using original EPUB '%s'",
                        resolved.name,
                    )
                    target_path = resolved
                    target_filename = resolved.name
                else:
                    logger.warning(
                        "⚡ Auto-Forge: STORYTELLER_NO_EPUB_CACHE=true but no original EPUB found; "
                        "falling back to Storyteller ReadAloud download"
                    )
                    no_epub_cache = False

            if not no_epub_cache:
                logger.info("Auto-Forge: Processing complete. Downloading artifact...")
                try:
                    if not st_client.download_book(book_uuid, target_path):
                        raise Exception("API download returned False")
                except Exception as api_err:
                    raise Exception(f"Failed to download Storyteller artifact: {api_err}")

            # --- RECALCULATE HASH ---
            if original_hash:
                logger.info(f"⚡ Auto-Forge: Preserving Original Hash: {original_hash}")
                new_hash = original_hash
            else:
                new_hash = self.ebook_parser.get_kosync_id(target_path)
                logger.info(f"⚡ Auto-Forge: Generated New Hash (Artifact): {new_hash}")

            # --- EXTRACT TEXT ---
            text_source_path = target_path
            if original_filename:
                original_candidates = []
                original_name = Path(str(original_ebook_filename or original_filename)).name
                if original_name:
                    original_candidates.append(self.ebook_parser.epub_cache_dir / original_name)

                source_path = text_item.get('path') if isinstance(text_item, dict) else None
                if source_path:
                    original_candidates.append(Path(source_path))

                try:
                    resolved_path = self.ebook_parser.resolve_book_path(original_name)
                    original_candidates.append(resolved_path)
                except Exception:
                    pass

                for candidate in original_candidates:
                    try:
                        if candidate and Path(candidate).exists():
                            text_source_path = Path(candidate)
                            break
                    except Exception:
                        continue

            book_text, _ = self.ebook_parser.extract_text_and_map(text_source_path)

            # --- INGEST STORYTELLER TRANSCRIPT (PRIMARY) ---
            storyteller_manifest = ingest_storyteller_transcripts(
                abs_id,
                title,
                chapters,
                storyteller_title=storyteller_title,
            )
            storyteller_alignment_ok = False
            transcript_source = None
            if storyteller_manifest:
                logger.info(f"Auto-Forge: Storyteller transcript ingested for '{abs_id}'")
                try:
                    st_transcript = StorytellerTranscript(storyteller_manifest)
                    if self.alignment_service.align_storyteller_and_store(abs_id, st_transcript, ebook_text=book_text):
                        storyteller_alignment_ok = True
                        transcript_source = "storyteller"
                        logger.info(f"Auto-Forge: Storyteller-anchored alignment map stored for '{abs_id}'")
                    else:
                        logger.warning(f"Auto-Forge: Storyteller alignment failed, falling back to SMIL for '{abs_id}'")
                except Exception as st_err:
                    logger.warning(f"Auto-Forge: Storyteller alignment error ({st_err}), falling back to SMIL for '{abs_id}'")
            else:
                logger.info(f"Auto-Forge: No Storyteller transcript files found for '{abs_id}'")

            # --- SMIL FALLBACK (LAST RESORT) ---
            if not storyteller_alignment_ok:
                logger.info("Auto-Forge: Falling back to SMIL transcript extraction...")
                raw_transcript = self.transcriber.transcribe_from_smil(
                    abs_id, target_path, chapters, full_book_text=book_text
                )
                if raw_transcript:
                    transcript_source = "smil"
                if not raw_transcript:
                    logger.info("Auto-Forge: SMIL unavailable/rejected. Falling back to Whisper transcription...")
                    whisper_audio = self._get_whisper_audio_inputs(temp_dir, abs_id, audio_source, audio_source_id)
                    if not whisper_audio:
                        source_name = "BookLore" if audio_source == "BookLore" and audio_source_id else "ABS"
                        raise Exception(
                            f"Auto-Forge: no audio files available for Whisper fallback (source={source_name})."
                        )
                    raw_transcript = self.transcriber.process_audio(
                        abs_id, whisper_audio, full_book_text=book_text
                    )
                    if raw_transcript:
                        transcript_source = "whisper"
                if not raw_transcript:
                    raise Exception("Auto-Forge: Failed to generate transcript from both SMIL and Whisper.")
                success = self.alignment_service.align_and_store(abs_id, raw_transcript, book_text, chapters)
                if not success:
                    raise Exception('Auto-Forge: align_and_store failed to generate a valid alignment map.')
                logger.info(f"Auto-Forge: {transcript_source.upper()} alignment map stored for '{abs_id}'")

            # --- UPDATE DATABASE ---
            book = self.database_service.get_book(abs_id)
            if book:
                book.ebook_filename = target_filename
                book.original_ebook_filename = original_ebook_filename
                book.storyteller_uuid = book_uuid
                book.kosync_doc_id = new_hash
                book.status = 'active'
                if storyteller_manifest and transcript_source == "storyteller":
                    book.transcript_file = storyteller_manifest
                    book.transcript_source = "storyteller"
                elif transcript_source:
                    book.transcript_source = transcript_source
                if not book.series_name:
                    try:
                        if audio_source != "BookLore" and item_details:
                            _meta = item_details.get("media", {}).get("metadata", {})
                            _sname, _sseq = _extract_series_from_abs_meta(_meta)
                            book.series_name = _sname
                            book.series_sequence = _sseq
                        elif audio_source == "BookLore" and audio_source_id and self.booklore_client:
                            _bl_detail = self.booklore_client.get_book_by_id(audio_source_id)
                            if _bl_detail:
                                _raw = _bl_detail.get("metadata") or _bl_detail
                                _sname, _sseq = _extract_series_from_abs_meta(_raw, booklore_mode=True)
                                book.series_name = _sname
                                book.series_sequence = _sseq
                    except Exception as _se:
                        logger.debug(f"Auto-Forge: could not capture series metadata: {_se}")
                self.database_service.save_book(book)
                logger.info(f"✅ Auto-Forge: Book {abs_id} updated successfully!")
            else:
                logger.error(f"❌ Auto-Forge: Book {abs_id} not found in DB to update!")

            # --- ADD TO COLLECTIONS/SHELVES ---
            try:
                abs_collection_name = os.environ.get("ABS_COLLECTION_NAME", "Synced with KOReader")
                if not str(abs_id).startswith('booklore:'):
                    self.abs_client.add_to_collection(abs_id, abs_collection_name)

                if self.booklore_client:
                    shelf_filename = original_filename if original_filename else target_filename
                    booklore_shelf_name = os.environ.get("BOOKLORE_SHELF_NAME", "Kobo")
                    self.booklore_client.add_to_shelf(shelf_filename, booklore_shelf_name)

                if self.storyteller_client:
                    if book_uuid and hasattr(self.storyteller_client, 'add_to_collection_by_uuid'):
                        self.storyteller_client.add_to_collection_by_uuid(book_uuid)
                    else:
                        self.storyteller_client.add_to_collection(target_filename)

            except Exception as e:
                logger.warning(f"⚠️ Auto-Forge: Failed to add to collections/shelves: {e}")

        except Exception as e:
            logger.error(f"❌ Auto-Forge: Pipeline failed: {e}", exc_info=True)
            try:
                book = self.database_service.get_book(abs_id)
                if book:
                    book.status = 'error'
                    self.database_service.save_book(book)
            except: pass

        finally:
            if temp_dir and temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)
            with self.lock:
                self.active_tasks.discard(title)

