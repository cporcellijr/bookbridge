import logging
import threading
import shutil
import time
import os
import html
import re
from pathlib import Path
from urllib.parse import urljoin
import requests

from src.services.alignment_service import ingest_storyteller_transcripts
from src.utils.storyteller_transcript import StorytellerTranscript

logger = logging.getLogger(__name__)
AUDIO_EXTENSIONS = {'.mp3', '.m4b', '.m4a', '.flac', '.ogg', '.opus', '.wma', '.wav', '.aac'}
DEFAULT_STAGE_MODE = "cleanup"
HARDLINK_STAGE_MODE = "hardlink"
VALID_STAGE_MODES = {DEFAULT_STAGE_MODE, HARDLINK_STAGE_MODE}

class ForgeService:
    def __init__(self, database_service, abs_client, booklore_client, storyteller_client, library_service, ebook_parser, transcriber, alignment_service):
        self.database_service = database_service
        self.abs_client = abs_client
        self.booklore_client = booklore_client
        self.storyteller_client = storyteller_client
        self.library_service = library_service
        self.ebook_parser = ebook_parser
        self.transcriber = transcriber
        self.alignment_service = alignment_service
        self.active_tasks = set()
        self.lock = threading.Lock()
        
        # Load environment variables
        self.ABS_API_TOKEN = os.environ.get("ABS_KEY")
        self.ABS_API_URL = os.environ.get("ABS_SERVER")
        self.ABS_AUDIO_ROOT = Path(os.environ.get("AUDIOBOOKS_DIR", "/audiobooks"))
        self.storyteller_cleanup_grace_seconds = self._safe_int_env("STORYTELLER_CLEANUP_GRACE_SECONDS", 120)
        self.storyteller_recovery_max_wait_seconds = self._safe_int_env("STORYTELLER_RECOVERY_MAX_WAIT_SECONDS", 21600)
        self.storyteller_recovery_poll_interval_seconds = max(
            30, self._safe_int_env("STORYTELLER_RECOVERY_POLL_INTERVAL_SECONDS", 120)
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

    def _discover_storyteller_uuid(self, st_client, safe_title: str, epub_filename: str, title: str):
        """
        Try to discover Storyteller UUID using staged path first, then title search.
        """
        try:
            found_uuid = st_client.find_book_by_staged_path(safe_title, epub_filename)
            if found_uuid:
                return found_uuid
        except Exception as e:
            logger.debug(f"Forge: staged-path UUID discovery failed: {e}")

        try:
            results = st_client.search_books(title) or []
            title_norm = self._normalize_storyteller_title(title)
            for book in results:
                book_title = self._normalize_storyteller_title(book.get('title', ''))
                if title_norm and book_title == title_norm:
                    return book.get('uuid') or book.get('id')
        except Exception as e:
            logger.debug(f"Forge: title-search UUID discovery failed: {e}")

        return None

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

    def _poll_auto_forge_completion(
        self,
        st_client,
        safe_title: str,
        epub_filename: str,
        title: str,
        course_dir: Path,
        epub_cache: Path,
        found_uuid: str,
        processing_triggered: bool,
        poll_count: int,
    ):
        """
        Execute one completion-poll cycle for auto-forge.
        """
        completion_method = None
        readaloud_path = None
        probe_download_path = None
        api_ready_seen = False
        details = None
        processing_ready = False
        processing_state = "not_checked"

        if not found_uuid:
            recovered_uuid = self._discover_storyteller_uuid(st_client, safe_title, epub_filename, title)
            if recovered_uuid:
                found_uuid = recovered_uuid
                logger.info(f"Auto-Forge: Recovered Storyteller UUID during wait loop: {found_uuid}")

        if found_uuid:
            details, processing_ready, processing_state = self._get_storyteller_processing_state(
                st_client, found_uuid
            )

        if found_uuid and not processing_triggered and processing_ready:
            try:
                st_client.trigger_processing(found_uuid)
                processing_triggered = True
            except Exception as trigger_err:
                logger.debug(f"Auto-Forge: trigger retry failed for {found_uuid}: {trigger_err}")
        elif found_uuid and not processing_triggered and poll_count % 4 == 0:
            logger.debug(
                f"Auto-Forge: delaying processing trigger for {found_uuid} "
                f"(Storyteller state={processing_state})"
            )

        readaloud_path = self._find_processed_epub(course_dir)
        if readaloud_path:
            completion_method = "local_readaloud"
            return {
                "found_uuid": found_uuid,
                "processing_triggered": processing_triggered,
                "readaloud_path": readaloud_path,
                "completion_method": completion_method,
                "probe_download_path": probe_download_path,
                "api_ready_seen": api_ready_seen,
            }

        if found_uuid:
            readaloud_meta = details.get("readaloud", {}) if isinstance(details, dict) else {}
            readaloud_filepath = readaloud_meta.get("filepath") if isinstance(readaloud_meta, dict) else None
            if readaloud_filepath:
                # Metadata can appear before the artifact is safely downloadable.
                # Track readiness for diagnostics, but do not mark completion yet.
                api_ready_seen = True

            if poll_count % 4 == 0:
                probe_path = epub_cache / f".storyteller_probe_{found_uuid}.epub"
                try:
                    if st_client.download_book(found_uuid, probe_path, polling=True):
                        if probe_path.exists() and probe_path.stat().st_size > 0:
                            probe_download_path = probe_path
                            api_ready_seen = True
                            completion_method = "api_download"
                except Exception as probe_err:
                    logger.debug(f"Auto-Forge: probe download not ready for {found_uuid}: {probe_err}")
                finally:
                    if completion_method != "api_download" and probe_path.exists():
                        try:
                            probe_path.unlink()
                        except Exception:
                            pass

        return {
            "found_uuid": found_uuid,
            "processing_triggered": processing_triggered,
            "readaloud_path": readaloud_path,
            "completion_method": completion_method,
            "probe_download_path": probe_download_path,
            "api_ready_seen": api_ready_seen,
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

    def _copy_booklore_audio_files(self, book_id: str, dest_folder: Path) -> bool:
        """Download audiobook tracks from Booklore into dest_folder."""
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
            info = self.booklore_client.get_audiobook_info(book_id)
            if not info:
                logger.warning(f"No audiobook info found for Booklore book '{book_id}'")
                return False

            logger.debug(f"Booklore audiobook info keys for '{book_id}': {list(info.keys())}")
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
                    f"No audio tracks found for Booklore book '{book_id}' "
                    f"(info keys: {list(info.keys())})"
                )
                return False
            logger.info(
                f"Booklore audio mode for '{book_id}': {track_mode} ({len(tracks)} stream item(s))"
            )

            dest_folder.mkdir(parents=True, exist_ok=True)
            downloaded = 0

            for idx, track in enumerate(tracks):
                ext = infer_ext(track, info)
                dest_path = dest_folder / f"track_{idx:03d}.{ext}"

                if track_mode == "chapter_markers_single_stream":
                    # For single M4B files with only chapter markers, the Booklore
                    # /track/{index}/stream endpoint uses the M4B's container stream
                    # index, where stream 0 is often the cover art (mjpeg), not the
                    # audio. Download the whole file instead.
                    logger.info(
                        f"Booklore audio (single-file): downloading whole file -> '{dest_path.name}'"
                    )
                    if self.booklore_client.download_book_to_path(
                        book_id, dest_path,
                        expected_size=int(info.get("totalSizeBytes") or 0),
                    ):
                        downloaded += 1
                    else:
                        logger.error(
                            f"Failed to download Booklore whole-file audio for book '{book_id}'"
                        )
                else:
                    download_index = track.get("index") if isinstance(track.get("index"), int) else idx
                    logger.info(
                        f"Booklore audio: downloading stream index {download_index} -> '{dest_path.name}'"
                    )
                    if self.booklore_client.download_audiobook_track(book_id, download_index, dest_path):
                        downloaded += 1
                    else:
                        logger.error(
                            f"Failed to download Booklore track index {download_index} for book '{book_id}'"
                        )

            if downloaded == len(tracks):
                logger.info(f"Booklore audio: downloaded all {downloaded} tracks for book '{book_id}'")
                return True
            else:
                logger.error(
                    f"Booklore audio: expected {len(tracks)} tracks, downloaded {downloaded} — Aborting"
                )
                return False
        except Exception as e:
            logger.error(f"Failed to copy Booklore audio for book '{book_id}': {e}", exc_info=True)
            return False

    def start_manual_forge(self, abs_id, text_item, title, author, stage_mode: str = DEFAULT_STAGE_MODE):
        """
        Start manual forge process in background thread.
        """
        normalized_stage_mode = self._normalize_stage_mode(stage_mode)
        thread_kwargs = {}
        if normalized_stage_mode != DEFAULT_STAGE_MODE:
            thread_kwargs["kwargs"] = {"stage_mode": normalized_stage_mode}
        thread = threading.Thread(
            target=self._forge_background_task,
            args=(abs_id, text_item, title, author),
            daemon=True,
            **thread_kwargs
        )
        thread.start()

    def _forge_background_task(self, abs_id, text_item, title, author, stage_mode: str = DEFAULT_STAGE_MODE):
        """
        Background thread: copy files to Storyteller library, trigger processing, cleanup.
        """
        logger.info(f"🔨 Forge: Starting background task for '{title}'")
        stage_mode = self._normalize_stage_mode(stage_mode)
        logger.info(f"Forge: Staging mode '{stage_mode}'")

        with self.lock:
            self.active_tasks.add(title)

        try:
            safe_author = self.safe_folder_name(author) if author else "Unknown"
            safe_title = self.safe_folder_name(title) if title else "Unknown"
            
            st_lib_path = Path(os.environ.get("STORYTELLER_LIBRARY_DIR", "/storyteller_library"))
            dest_base = Path(os.environ.get("PROCESSING_DIR", "/tmp"))

            final_course_dir = st_lib_path / safe_title
            hidden_staging_dir = st_lib_path / f".staging_{safe_title}"
            processing_dir = dest_base / f"forge_staging_{safe_title}"

            if final_course_dir.exists():
                logger.warning(f"⚠️ Target directory '{final_course_dir}' already exists. Using it directly")
                course_dir = final_course_dir
            else:
                course_dir = processing_dir
                course_dir.mkdir(parents=True, exist_ok=True)

            audio_dest = course_dir
            
            logger.info(f"⚡ Forge: Staging files for '{title}' in '{course_dir}' (Atomic)")

            # Step 1: Copy audio files
            audio_ok = self._copy_audio_files(abs_id, audio_dest, stage_mode=stage_mode)
            if not audio_ok:
                logger.error(f"❌ Forge: Failed to copy audio files for '{abs_id}'")
                try:
                    if course_dir.exists() and course_dir != final_course_dir: 
                        shutil.rmtree(course_dir) 
                except: pass
                return
            logger.info(f"⚡ Forge: Audio files copied for '{title}'")

            # Step 2: Acquire text source (epub)
            epub_dest = course_dir / f"{safe_title}.epub"
            source = text_item.get('source', '')
            
            text_success = False

            if source == 'Local File':
                src_path = Path(text_item.get('path', ''))
                if src_path.exists():
                    self._stage_local_file(src_path, epub_dest, stage_mode, "Forge")
                    text_success = True
                    logger.info(f"⚡ Forge: Local epub copied: {src_path.name}")
                else:
                    logger.error(f"❌ Forge: Local file not found: '{src_path}'")

            elif source == 'Booklore':
                booklore_id = text_item.get('booklore_id')
                if booklore_id:
                    content = self.booklore_client.download_book(booklore_id)
                    if content:
                        epub_dest.write_bytes(content)
                        text_success = True
                        logger.info(f"⚡ Forge: Booklore epub downloaded")
                    else:
                        logger.error(f"❌ Forge: Booklore download failed for '{booklore_id}'")

            elif source == 'ABS':
                abs_item_id = text_item.get('abs_id')
                if abs_item_id:
                    ebook_files = self.abs_client.get_ebook_files(abs_item_id)
                    if ebook_files:
                        stream_url = ebook_files[0].get('stream_url', '')
                        if stream_url and self.abs_client.download_file(stream_url, epub_dest):
                            text_success = True
                            logger.info(f"⚡ Forge: ABS epub downloaded")
                        else:
                            logger.error(f"❌ Forge: ABS download failed for '{abs_item_id}'")
            
            elif source == 'CWA':
                download_url = text_item.get('download_url', '')
                cwa_id = text_item.get('cwa_id')
                cwa_client = self.library_service.cwa_client
                
                if download_url and cwa_client:
                    if cwa_client.download_ebook(download_url, epub_dest):
                        text_success = True
                        logger.info(f"⚡ Forge: CWA epub downloaded")
                elif cwa_id and cwa_client:
                    book_info = cwa_client.get_book_by_id(cwa_id)
                    if book_info and book_info.get('download_url'):
                        if cwa_client.download_ebook(book_info['download_url'], epub_dest):
                            text_success = True
                            logger.info(f"⚡ Forge: CWA epub downloaded via ID lookup")
                
                if not text_success:
                    logger.error(f"❌ Forge: CWA download failed")

            else:
                logger.error(f"❌ Forge: Unknown text source: '{source}'")

            if not text_success:
                logger.error(f"❌ Forge: Text acquisition failed — Aborting")
                try:
                    if course_dir.exists() and course_dir != final_course_dir:
                        shutil.rmtree(course_dir)
                except: pass
                return

            # TWO-STEP ATOMIC TRANSFER
            if course_dir != final_course_dir:
                try:
                    logger.info(f"⚡ Forge: Transferring to Storyteller volume as hidden folder...")
                    if hidden_staging_dir.exists():
                        shutil.rmtree(hidden_staging_dir)
                    if final_course_dir.exists():
                        shutil.rmtree(final_course_dir)

                    # Step 1: Cross-device move to hidden folder inside Storyteller library
                    shutil.move(str(course_dir), str(hidden_staging_dir))
                    logger.info(f"⚡ Forge: Atomically revealing folder to Storyteller scanner...")
                    hidden_staging_dir.rename(final_course_dir)
                    course_dir = final_course_dir
                except Exception as e:
                    logger.error(f"❌ Forge: Atomic transfer failed: {e}")
                    try: shutil.rmtree(course_dir)
                    except: pass
                    try: shutil.rmtree(hidden_staging_dir)
                    except: pass
                    raise Exception(f"Atomic move failed: {e}")

            logger.info(f"⚡ Forge: Files staged. Waiting for Storyteller to detect '{title}'...")

            # Trigger Storyteller Processing via API
            st_client = self.storyteller_client
            found_uuid = None
            epub_filename = f"{safe_title}.epub"
            ready = False

            for _ in range(240):
                time.sleep(5)
                try:
                    # Primary: match by staged file path (deterministic)
                    found_uuid = st_client.find_book_by_staged_path(safe_title, epub_filename)

                    # Fallback: title search
                    if not found_uuid:
                        results = st_client.search_books(title)
                        for b in results:
                            if b.get('title') == title:
                                found_uuid = b.get('uuid') or b.get('id')
                                break

                    if found_uuid:
                        logger.info(
                            f"⚡ Forge: Book detected ({found_uuid}). Waiting for Storyteller API readiness..."
                        )
                        ready_details = None
                        ready_state = "not_visible"
                        for ready_poll in range(120):
                            ready_details, ready, ready_state = self._get_storyteller_processing_state(
                                st_client, found_uuid
                            )
                            if ready:
                                break
                            if ready_poll and ready_poll % 12 == 0:
                                logger.debug(
                                    f"Forge: Storyteller book {found_uuid} not ready yet "
                                    f"(state={ready_state})"
                                )
                            time.sleep(5)
                        else:
                            ready = False

                        if ready:
                            logger.info(f"⚡ Forge: Storyteller book ready for processing ({found_uuid})")
                        else:
                            logger.warning(
                                f"⚠️ Forge: Storyteller book detected ({found_uuid}) but never became "
                                f"API-ready for processing (state={ready_state})"
                            )
                        break
                except Exception as e:
                    logger.debug(f"Forge: Storyteller detection error (retrying): {e}")

            if found_uuid and ready:
                logger.info(f"⚡ Forge: Book detected ({found_uuid}). Triggering processing...")
                try:
                    if hasattr(st_client, 'trigger_processing'):
                        st_client.trigger_processing(found_uuid)
                    else:
                        logger.warning("⚠️ Storyteller client missing trigger_processing method")
                except Exception as e:
                     logger.error(f"❌ Forge: Failed to trigger processing: {e}")
            elif found_uuid:
                logger.warning(
                    f"⚠️ Forge: Storyteller book detected ({found_uuid}) before API readiness; "
                    "skipping explicit trigger and waiting for recovery polling"
                )
            else:
                logger.warning(f"⚠️ Forge: Storyteller scan timed out — Processing might happen automatically later")


            # Step 3: Cleanup Monitor
            MAX_WAIT = 3600  # 60 minutes
            POLL_INTERVAL = 30 # Check every 30s
            elapsed = 0

            logger.info(f"⚡ Forge: Starting cleanup monitor (polling every {POLL_INTERVAL}s, max {MAX_WAIT}s)")

            while elapsed < MAX_WAIT:
                time.sleep(POLL_INTERVAL)
                elapsed += POLL_INTERVAL

                try:
                    processed_epub = self._find_processed_epub(course_dir)
                    
                    if processed_epub:
                        logger.info(f"⚡ Forge: Readaloud detected: {processed_epub.name}")

                        # [SAFETY CHECK]
                        if found_uuid:
                            try:
                                logger.info(f"⚡ Forge: Verifying processing status for {found_uuid}...")
                                for _ in range(12): 
                                    details = st_client.get_book_details(found_uuid)
                                    time.sleep(5)
                                
                                logger.info("⚡ Forge: Safety delay (60s) to allow Storyteller to release file locks...")
                                time.sleep(60) 
                            except Exception as e:
                                logger.warning(f"⚠️ Forge: Safety check failed: {e} — Proceeding with caution")
                                time.sleep(30)

                        # --- EXTRACT & ALIGN ---
                        completed_epub_path = processed_epub
                        try:
                            logger.info(f"⚡ Forge: Extracting SMIL transcript from {completed_epub_path.name}...")
                            item_details = self.abs_client.get_item_details(abs_id)
                            chapters = item_details.get('media', {}).get('chapters', []) if item_details else []
                            book_text, _ = self.ebook_parser.extract_text_and_map(completed_epub_path)
                            raw_transcript = self.transcriber.transcribe_from_smil(
                                abs_id, completed_epub_path, chapters, full_book_text=book_text
                            )
                            if not raw_transcript:
                                logger.error(f"❌ Forge: SMIL extraction returned no transcript for '{abs_id}' — Alignment map not created")
                            else:
                                success = self.alignment_service.align_and_store(abs_id, raw_transcript, book_text, chapters)
                                if not success:
                                    logger.error(f"❌ Forge: align_and_store failed for '{abs_id}' — Alignment map not created")
                                else:
                                    logger.info(f"✅ Forge: Alignment map stored for '{abs_id}'")
                        except Exception as e:
                            logger.error(f"❌ Forge: Alignment extraction failed: {e}")

                        if self.storyteller_cleanup_grace_seconds > 0:
                            logger.info(
                                f"Forge: Grace wait before cleanup: {self.storyteller_cleanup_grace_seconds}s"
                            )
                            time.sleep(self.storyteller_cleanup_grace_seconds)

                        if self._should_cleanup_staged_sources(stage_mode):
                            self._cleanup_staged_sources(
                                course_dir=course_dir,
                                staged_epub_path=epub_dest,
                                preserve_paths=[completed_epub_path],
                                context="Forge",
                            )
                        else:
                            logger.info(
                                "Forge: Keeping staged source files because staging mode '%s' disables cleanup",
                                stage_mode,
                            )

                        return

                except Exception as e:
                    logger.warning(f"⚠️ Forge: Cleanup monitor error: {e}")

            logger.warning(f"⚠️ Forge: Cleanup monitor timed out after {MAX_WAIT}s for '{title}' — Source files remain")

        except Exception as e:
            logger.error(f"❌ Forge: Background task failed for '{title}': {e}", exc_info=True)
        finally:
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
        Staging -> Trigger -> Wait -> Download -> Sanitize -> Recalc Hash -> Update DB -> Cleanup
        """
        logger.info(f"🔨 Auto-Forge: Starting pipeline for '{title}' (ABS {abs_id})")
        
        with self.lock:
            self.active_tasks.add(title)

        stage_mode = self._normalize_stage_mode(stage_mode)
        logger.info(f"Auto-Forge: Staging mode '{stage_mode}'")

        course_dir = None
        epub_dest = None
        cleanup_requested = False
        cleanup_preserve_paths = []

        try:
            original_ebook_filename = self._extract_original_filename(text_item, original_filename)

            # --- STAGING & TRIGGER ---
            safe_author = self.safe_folder_name(author) if author else "Unknown"
            safe_title = self.safe_folder_name(title) if title else "Unknown"
            st_lib_path = Path(os.environ.get("STORYTELLER_LIBRARY_DIR", "/storyteller_library"))
            dest_base = Path(os.environ.get("PROCESSING_DIR", "/tmp"))

            final_course_dir = st_lib_path / safe_title
            hidden_staging_dir = st_lib_path / f".staging_{safe_title}"
            processing_dir = dest_base / f"forge_staging_{safe_title}"

            if final_course_dir.exists():
                logger.warning(f"⚠️ Target directory '{final_course_dir}' already exists. Using it directly")
                course_dir = final_course_dir
            else:
                course_dir = processing_dir
                course_dir.mkdir(parents=True, exist_ok=True)
            
            # Copy Audio
            if audio_source == 'BookLore' and audio_source_id:
                if not self._copy_booklore_audio_files(audio_source_id, course_dir):
                    raise Exception("Failed to copy Booklore audio files")
            else:
                if not self._copy_audio_files(abs_id, course_dir, stage_mode=stage_mode):
                    raise Exception("Failed to copy audio files")
                
            # Copy Text
            epub_dest = course_dir / f"{safe_title}.epub"
            source = text_item.get('source')
            if source == 'Local File':
                self._stage_local_file(text_item.get('path'), epub_dest, stage_mode, "Auto-Forge")
            elif source == 'Booklore':
                content = self.booklore_client.download_book(text_item.get('booklore_id'))
                if content: epub_dest.write_bytes(content)
            elif source == 'ABS':
                 ebook_files = self.abs_client.get_ebook_files(text_item.get('abs_id'))
                 if ebook_files: self.abs_client.download_file(ebook_files[0]['stream_url'], epub_dest)
            elif source == 'CWA':
                 cwa_client = getattr(self.library_service, 'cwa_client', None)
                 download_url = text_item.get('download_url')
                 cwa_id = text_item.get('cwa_id')
                 text_downloaded = False
                 if download_url and cwa_client:
                     text_downloaded = bool(cwa_client.download_ebook(download_url, epub_dest))
                 elif cwa_id and cwa_client:
                     book_info = cwa_client.get_book_by_id(cwa_id)
                     if book_info and book_info.get('download_url'):
                         text_downloaded = bool(cwa_client.download_ebook(book_info['download_url'], epub_dest))
                 if not text_downloaded:
                     logger.error(f"❌ Auto-Forge: CWA download failed for '{cwa_id or download_url or 'unknown'}'")
            else:
                 raise Exception(f"Unknown or missing text source type: '{source}'")
            
            if not epub_dest.exists():
                raise Exception("Failed to acquire text source")

            # TWO-STEP ATOMIC TRANSFER
            if course_dir != final_course_dir:
                try:
                    logger.info(f"⚡ Forge: Transferring to Storyteller volume as hidden folder...")
                    if hidden_staging_dir.exists():
                        shutil.rmtree(hidden_staging_dir)
                    if final_course_dir.exists():
                        shutil.rmtree(final_course_dir)

                    # Step 1: Cross-device move to hidden folder inside Storyteller library
                    shutil.move(str(course_dir), str(hidden_staging_dir))
                    logger.info(f"⚡ Forge: Atomically revealing folder to Storyteller scanner...")
                    hidden_staging_dir.rename(final_course_dir)
                    course_dir = final_course_dir
                except Exception as e:
                    logger.error(f"❌ Forge: Atomic transfer failed: {e}")
                    try: shutil.rmtree(course_dir)
                    except: pass
                    try: shutil.rmtree(hidden_staging_dir)
                    except: pass
                    raise Exception(f"Atomic move failed: {e}")

            logger.info("⚡ Auto-Forge: Files staged. Waiting for Storyteller detection...")

            # Trigger Storyteller
            st_client = self.storyteller_client
            found_uuid = None
            epub_filename = f"{safe_title}.epub"

            processing_triggered = False
            ready = False
            ready_state = "not_detected"
            for _ in range(240):  # Wait up to 20 mins for initial detection
                time.sleep(5)
                found_uuid = self._discover_storyteller_uuid(st_client, safe_title, epub_filename, title)
                if found_uuid:
                    logger.info(
                        f"Forge: Book detected ({found_uuid}). Waiting for Storyteller API readiness..."
                    )
                    for ready_poll in range(120):
                        _ready_details, ready, ready_state = self._get_storyteller_processing_state(
                            st_client, found_uuid
                        )
                        if ready:
                            break
                        if ready_poll and ready_poll % 12 == 0:
                            logger.debug(
                                f"Auto-Forge: Storyteller book {found_uuid} not ready yet "
                                f"(state={ready_state})"
                            )
                        time.sleep(5)
                    else:
                        ready = False

                    if ready:
                        logger.info(f"Auto-Forge: Storyteller book ready for processing ({found_uuid})")
                    else:
                        logger.warning(
                            f"Auto-Forge: Storyteller book detected ({found_uuid}) but never became "
                            f"API-ready for processing (state={ready_state})"
                        )
                    break

            if found_uuid and ready:
                logger.info(f"Auto-Forge: Triggering processing for {found_uuid}")
                try:
                    st_client.trigger_processing(found_uuid)
                    processing_triggered = True
                except Exception as trigger_err:
                    logger.warning(f"Auto-Forge: Failed to trigger processing for {found_uuid}: {trigger_err}")
            elif found_uuid:
                logger.warning(
                    f"Auto-Forge: Storyteller book detected ({found_uuid}) before API readiness; "
                    "skipping explicit trigger and continuing with recovery polling"
                )
            else:
                logger.warning("Auto-Forge: Storyteller scan timed out - continuing with recovery polling")

            # --- WAIT FOR COMPLETION ---
            MAX_WAIT = 3600
            POLL_INTERVAL = 30
            elapsed = 0
            poll_count = 0
            readaloud_path = None
            completion_method = None
            api_ready_seen = False
            probe_download_path = None

            epub_cache = self.ebook_parser.epub_cache_dir
            if not epub_cache.exists():
                epub_cache.mkdir(parents=True, exist_ok=True)

            while elapsed < MAX_WAIT:
                time.sleep(POLL_INTERVAL)
                elapsed += POLL_INTERVAL
                poll_count += 1

                poll_result = self._poll_auto_forge_completion(
                    st_client=st_client,
                    safe_title=safe_title,
                    epub_filename=epub_filename,
                    title=title,
                    course_dir=course_dir,
                    epub_cache=epub_cache,
                    found_uuid=found_uuid,
                    processing_triggered=processing_triggered,
                    poll_count=poll_count,
                )
                found_uuid = poll_result["found_uuid"]
                processing_triggered = poll_result["processing_triggered"]
                readaloud_path = poll_result["readaloud_path"] or readaloud_path
                probe_download_path = poll_result["probe_download_path"] or probe_download_path
                api_ready_seen = api_ready_seen or poll_result["api_ready_seen"]
                completion_method = poll_result["completion_method"]
                if completion_method:
                    break

            if not completion_method:
                timeout_reason = []
                if not found_uuid:
                    timeout_reason.append("no_uuid")
                if not self._find_processed_epub(course_dir):
                    timeout_reason.append("no_artifact_local")
                if found_uuid and not api_ready_seen:
                    timeout_reason.append("api_not_ready")
                reason_str = ",".join(timeout_reason) if timeout_reason else "unknown"

                logger.warning(
                    f"Auto-Forge timeout: abs_id={abs_id} elapsed={elapsed}s polls={poll_count} reason={reason_str} "
                    f"found_uuid={bool(found_uuid)} api_ready_seen={api_ready_seen}"
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
                        safe_title=safe_title,
                        epub_filename=epub_filename,
                        title=title,
                        course_dir=course_dir,
                        epub_cache=epub_cache,
                        found_uuid=found_uuid,
                        processing_triggered=processing_triggered,
                        poll_count=poll_count,
                    )
                    found_uuid = poll_result["found_uuid"]
                    processing_triggered = poll_result["processing_triggered"]
                    readaloud_path = poll_result["readaloud_path"] or readaloud_path
                    probe_download_path = poll_result["probe_download_path"] or probe_download_path
                    api_ready_seen = api_ready_seen or poll_result["api_ready_seen"]
                    completion_method = poll_result["completion_method"]

                if not completion_method:
                    logger.warning(
                        f"Auto-Forge: extended recovery timed out for abs_id={abs_id} "
                        f"elapsed={recovery_elapsed}s; keeping status='forging'"
                    )
                    return

            completion_msg = f"Auto-Forge: Completion detected via {completion_method}"
            if readaloud_path:
                completion_msg += f" ({readaloud_path})"
            logger.info(completion_msg)

            # Grace wait before download/cleanup to let Storyteller finish internal writes.
            if self.storyteller_cleanup_grace_seconds > 0:
                logger.info(
                    f"Auto-Forge: Grace wait before download/cleanup: {self.storyteller_cleanup_grace_seconds}s"
                )
                time.sleep(self.storyteller_cleanup_grace_seconds)

            # --- DOWNLOAD ---
            logger.info("Auto-Forge: Processing complete. Downloading artifact...")
            target_filename = f"storyteller_{found_uuid or abs_id}.epub"
            target_path = epub_cache / target_filename

            if probe_download_path and probe_download_path.exists():
                shutil.move(str(probe_download_path), str(target_path))
            elif found_uuid:
                try:
                    if not st_client.download_book(found_uuid, target_path):
                        raise Exception("API download returned False")
                except Exception as api_err:
                    if readaloud_path and readaloud_path.exists():
                        logger.warning(f"Auto-Forge: API download failed ({api_err}). Using local file: {readaloud_path}")
                        shutil.copy2(readaloud_path, target_path)
                    else:
                        raise Exception(f"Failed to download Storyteller artifact and no local fallback available: {api_err}")
            elif readaloud_path and readaloud_path.exists():
                shutil.copy2(readaloud_path, target_path)
            else:
                raise Exception("Auto-Forge completion detected but no downloadable artifact source was available")

            cleanup_requested = self._should_cleanup_staged_sources(stage_mode)
            if readaloud_path:
                cleanup_preserve_paths.append(readaloud_path)


            # --- RECALCULATE HASH ---
            # [FIX] Prioritize original_hash if valid (Tri-Link Principle)
            if original_hash:
                 logger.info(f"⚡ Auto-Forge: Preserving Original Hash: {original_hash}")
                 new_hash = original_hash
            else:
                 new_hash = self.ebook_parser.get_kosync_id(target_path)
                 logger.info(f"⚡ Auto-Forge: Generated New Hash (Artifact): {new_hash}")

            # --- EXTRACT TEXT ---
            item_details = self.abs_client.get_item_details(abs_id)
            chapters = item_details.get('media', {}).get('chapters', []) if item_details else []
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
            storyteller_manifest = ingest_storyteller_transcripts(abs_id, title, chapters)
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
                    audio_files = self.abs_client.get_audio_files(abs_id)
                    if not audio_files:
                        raise Exception("Auto-Forge: ABS returned no audio files for Whisper fallback.")
                    raw_transcript = self.transcriber.process_audio(
                        abs_id, audio_files, full_book_text=book_text
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
            # NOTE: DB service calls need connection. Assuming database_service handles its own session.
            book = self.database_service.get_book(abs_id)
            if book:
                book.ebook_filename = target_filename
                book.original_ebook_filename = original_ebook_filename
                book.storyteller_uuid = found_uuid
                book.kosync_doc_id = new_hash
                book.status = 'active'
                if storyteller_manifest and transcript_source == "storyteller":
                    book.transcript_file = storyteller_manifest
                    book.transcript_source = "storyteller"
                elif transcript_source:
                    book.transcript_source = transcript_source
                self.database_service.save_book(book)
                logger.info(f"✅ Auto-Forge: Book {abs_id} updated successfully!")
            else:
                logger.error(f"❌ Auto-Forge: Book {abs_id} not found in DB to update!")

            # --- ADD TO COLLECTIONS/SHELVES ---
            try:
                abs_collection_name = os.environ.get("ABS_COLLECTION_NAME", "Synced with KOReader")
                self.abs_client.add_to_collection(abs_id, abs_collection_name)

                if self.booklore_client:
                    shelf_filename = original_filename if original_filename else target_filename
                    booklore_shelf_name = os.environ.get("BOOKLORE_SHELF_NAME", "Kobo")
                    self.booklore_client.add_to_shelf(shelf_filename, booklore_shelf_name)

                if self.storyteller_client:
                    if found_uuid and hasattr(self.storyteller_client, 'add_to_collection_by_uuid'):
                        self.storyteller_client.add_to_collection_by_uuid(found_uuid)
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
            try:
                if cleanup_requested and course_dir and epub_dest:
                    self._cleanup_staged_sources(
                        course_dir=course_dir,
                        staged_epub_path=epub_dest,
                        preserve_paths=cleanup_preserve_paths,
                        context="Auto-Forge",
                    )
                elif course_dir and epub_dest:
                    logger.info(
                        "Auto-Forge: Keeping staged source files because staging mode '%s' disables cleanup",
                        stage_mode,
                    )
            except Exception as cleanup_err:
                logger.warning(f"Auto-Forge: Final cleanup failed: {cleanup_err}")

            with self.lock:
                self.active_tasks.discard(title)

