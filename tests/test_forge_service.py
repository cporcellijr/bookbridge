import unittest
from contextlib import ExitStack
from unittest.mock import MagicMock, patch, ANY
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.services.forge_service import ForgeService


class TestForgeService(unittest.TestCase):
    def setUp(self):
        self.mock_db = MagicMock()
        self.mock_abs = MagicMock()
        self.mock_booklore = MagicMock()
        self.mock_storyteller = MagicMock()
        self.mock_library = MagicMock()
        self.mock_cwa = MagicMock()
        self.mock_library.cwa_client = self.mock_cwa
        self.mock_ebook_parser = MagicMock()
        self.mock_transcriber = MagicMock()
        self.mock_alignment = MagicMock()
        
        self.service = ForgeService(
            database_service=self.mock_db,
            abs_client=self.mock_abs,
            booklore_client=self.mock_booklore,
            storyteller_client=self.mock_storyteller,
            library_service=self.mock_library,
            ebook_parser=self.mock_ebook_parser,
            transcriber=self.mock_transcriber,
            alignment_service=self.mock_alignment
        )
        
        # Suppress logging during tests
        self.logger_patch = patch('src.services.forge_service.logger')
        self.logger_patch.start()

    def tearDown(self):
        patch.stopall()

    def test_start_manual_forge(self):
        """Test starting a manual forge process."""
        # We process start_manual_forge which creates a thread targeting _forge_background_task
        with patch('threading.Thread') as mock_thread_cls:
            mock_thread_instance = MagicMock()
            mock_thread_cls.return_value = mock_thread_instance
            
            self.service.start_manual_forge(
                abs_id="abs456",
                text_item={"path": "other.epub"},
                title="Test Book 2",
                author="Test Author 2"
            )
            
            mock_thread_cls.assert_called_with(
                target=self.service._forge_background_task,
                args=("abs456", {"path": "other.epub"}, "Test Book 2", "Test Author 2"),
                daemon=True
            )
            mock_thread_instance.start.assert_called_once()

    def test_start_manual_forge_hardlink_passes_stage_mode_kwargs(self):
        with patch('threading.Thread') as mock_thread_cls:
            mock_thread_instance = MagicMock()
            mock_thread_cls.return_value = mock_thread_instance

            self.service.start_manual_forge(
                abs_id="abs456",
                text_item={"path": "other.epub"},
                title="Test Book 2",
                author="Test Author 2",
                stage_mode="hardlink",
            )

            mock_thread_cls.assert_called_with(
                target=self.service._forge_background_task,
                args=("abs456", {"path": "other.epub"}, "Test Book 2", "Test Author 2"),
                kwargs={"stage_mode": "hardlink"},
                daemon=True
            )
            mock_thread_instance.start.assert_called_once()

    def test_start_manual_forge_booklore_audio_passes_audio_kwargs(self):
        with patch('threading.Thread') as mock_thread_cls:
            mock_thread_instance = MagicMock()
            mock_thread_cls.return_value = mock_thread_instance

            self.service.start_manual_forge(
                abs_id="booklore:42",
                text_item={"path": "other.epub"},
                title="BookLore Audio",
                author="Test Author 2",
                audio_source="BookLore",
                audio_source_id="42",
            )

            mock_thread_cls.assert_called_with(
                target=self.service._forge_background_task,
                args=("booklore:42", {"path": "other.epub"}, "BookLore Audio", "Test Author 2"),
                kwargs={"audio_source": "BookLore", "audio_source_id": "42"},
                daemon=True
            )
            mock_thread_instance.start.assert_called_once()

    def test_start_auto_forge_match(self):
        """Test starting auto forge match."""
        # Using mock threading
        with patch('threading.Thread') as mock_thread_cls:
            mock_thread_instance = MagicMock()
            mock_thread_cls.return_value = mock_thread_instance
            
            self.service.start_auto_forge_match(
                abs_id="abs789",
                text_item={"booklore_id": 1},
                title="Auto Book",
                author="Auto Author",
                original_filename="orig.epub",
                original_hash="hash123"
            )
            
            mock_thread_cls.assert_called_with(
                target=self.service._auto_forge_background_task,
                args=("abs789", {"booklore_id": 1}, "Auto Book", "Auto Author", "orig.epub", "hash123",
                      None, None),
                daemon=True
            )
            mock_thread_instance.start.assert_called_once()

    def test_start_auto_forge_match_hardlink_passes_stage_mode_kwargs(self):
        with patch('threading.Thread') as mock_thread_cls:
            mock_thread_instance = MagicMock()
            mock_thread_cls.return_value = mock_thread_instance

            self.service.start_auto_forge_match(
                abs_id="abs789",
                text_item={"booklore_id": 1},
                title="Auto Book",
                author="Auto Author",
                original_filename="orig.epub",
                original_hash="hash123",
                stage_mode="hardlink",
            )

            mock_thread_cls.assert_called_with(
                target=self.service._auto_forge_background_task,
                args=("abs789", {"booklore_id": 1}, "Auto Book", "Auto Author", "orig.epub", "hash123",
                      None, None),
                kwargs={"stage_mode": "hardlink"},
                daemon=True
            )
            mock_thread_instance.start.assert_called_once()

    def test_stage_local_file_prefers_hardlink_when_requested(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source = tmp_path / "source.epub"
            dest = tmp_path / "dest.epub"
            source.write_bytes(b"source")

            with patch("src.services.forge_service.os.link") as mock_link, patch(
                "src.services.forge_service.shutil.copy2"
            ) as mock_copy:
                result = self.service._stage_local_file(source, dest, "hardlink", "Forge")

            self.assertEqual(result, "hardlink")
            mock_link.assert_called_once_with(source, dest)
            mock_copy.assert_not_called()

    def test_stage_local_file_hardlink_falls_back_to_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source = tmp_path / "source.epub"
            dest = tmp_path / "dest.epub"
            source.write_bytes(b"source")

            with patch("src.services.forge_service.os.link", side_effect=OSError("no hardlink")), patch(
                "src.services.forge_service.shutil.copy2"
            ) as mock_copy:
                result = self.service._stage_local_file(source, dest, "hardlink", "Forge")

            self.assertEqual(result, "copy")
            mock_copy.assert_called_once_with(str(source), dest)

    def test_booklore_hardlink_uses_local_exact_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source = tmp_path / "exact.m4b"
            source.write_bytes(b"audio")
            dest_folder = tmp_path / "dest"
            self.service.ABS_AUDIO_ROOT = tmp_path
            self.mock_booklore.get_audiobook_info.return_value = {
                "tracks": [{"index": 0, "fileName": source.name, "extension": "m4b"}]
            }
            self.mock_booklore.get_book_by_id.return_value = {
                "id": "bl-1",
                "alternativeFormats": [
                    {"bookType": "AUDIOBOOK", "fileName": source.name, "filePath": str(source)}
                ],
            }

            with patch.object(self.service, "_stage_local_file", return_value="hardlink") as mock_stage:
                result = self.service._copy_booklore_audio_files("bl-1", dest_folder, stage_mode="hardlink")

            self.assertTrue(result)
            mock_stage.assert_called_once_with(
                source,
                dest_folder / "track_000.m4b",
                "hardlink",
                "Grimmory audio",
            )
            self.mock_booklore.download_book_to_path.assert_not_called()
            self.mock_booklore.download_audiobook_track.assert_not_called()

    def test_booklore_hardlink_uses_suffix_matched_local_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            local_root = tmp_path / "audiobooks"
            source = local_root / "Author" / "Series" / "suffix.m4b"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(b"audio")
            dest_folder = tmp_path / "dest"
            self.service.ABS_AUDIO_ROOT = local_root
            self.mock_booklore.get_audiobook_info.return_value = {
                "tracks": [{"index": 0, "fileName": source.name, "extension": "m4b"}]
            }
            self.mock_booklore.get_book_by_id.return_value = {
                "id": "bl-1",
                "alternativeFormats": [
                    {
                        "bookType": "AUDIOBOOK",
                        "fileName": source.name,
                        "filePath": "/srv/booklore/Author/Series/suffix.m4b",
                    }
                ],
            }

            with patch.object(self.service, "_stage_local_file", return_value="hardlink") as mock_stage:
                result = self.service._copy_booklore_audio_files("bl-1", dest_folder, stage_mode="hardlink")

            self.assertTrue(result)
            mock_stage.assert_called_once_with(
                source,
                dest_folder / "track_000.m4b",
                "hardlink",
                "Grimmory audio",
            )
            self.mock_booklore.download_audiobook_track.assert_not_called()

    def test_booklore_hardlink_uses_filename_glob_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            local_root = tmp_path / "audiobooks"
            source = local_root / "nested" / "globbed.m4b"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(b"audio")
            dest_folder = tmp_path / "dest"
            self.service.ABS_AUDIO_ROOT = local_root
            self.mock_booklore.get_audiobook_info.return_value = {
                "tracks": [{"index": 0, "fileName": source.name, "extension": "m4b"}]
            }
            self.mock_booklore.get_book_by_id.return_value = {
                "id": "bl-1",
                "alternativeFormats": [
                    {
                        "bookType": "AUDIOBOOK",
                        "fileName": source.name,
                        "filePath": "/missing/location/not-the-same.m4b",
                    }
                ],
            }

            with patch.object(self.service, "_stage_local_file", return_value="hardlink") as mock_stage:
                result = self.service._copy_booklore_audio_files("bl-1", dest_folder, stage_mode="hardlink")

            self.assertTrue(result)
            mock_stage.assert_called_once_with(
                source,
                dest_folder / "track_000.m4b",
                "hardlink",
                "Grimmory audio",
            )
            self.mock_booklore.download_audiobook_track.assert_not_called()

    def test_booklore_hardlink_falls_back_to_copy_when_link_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source = tmp_path / "copy-fallback.m4b"
            source.write_bytes(b"audio")
            dest_folder = tmp_path / "dest"
            self.service.ABS_AUDIO_ROOT = tmp_path
            self.mock_booklore.get_audiobook_info.return_value = {
                "tracks": [{"index": 0, "fileName": source.name, "extension": "m4b"}]
            }
            self.mock_booklore.get_book_by_id.return_value = {
                "id": "bl-1",
                "alternativeFormats": [
                    {"bookType": "AUDIOBOOK", "fileName": source.name, "filePath": str(source)}
                ],
            }

            with patch("src.services.forge_service.os.link", side_effect=OSError("no hardlink")), patch(
                "src.services.forge_service.shutil.copy2"
            ) as mock_copy:
                result = self.service._copy_booklore_audio_files("bl-1", dest_folder, stage_mode="hardlink")

            self.assertTrue(result)
            mock_copy.assert_called_once_with(str(source), dest_folder / "track_000.m4b")
            self.mock_booklore.download_book_to_path.assert_not_called()
            self.mock_booklore.download_audiobook_track.assert_not_called()

    def test_booklore_hardlink_falls_back_to_download_when_local_resolution_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            local_root = tmp_path / "audiobooks"
            existing = local_root / "track_a.m4b"
            existing.parent.mkdir(parents=True, exist_ok=True)
            existing.write_bytes(b"a")
            dest_folder = tmp_path / "dest"
            self.service.ABS_AUDIO_ROOT = local_root
            self.mock_booklore.get_audiobook_info.return_value = {
                "tracks": [
                    {"index": 0, "fileName": "track_a.m4b", "extension": "m4b"},
                    {"index": 1, "fileName": "track_b.m4b", "extension": "m4b"},
                ]
            }
            self.mock_booklore.get_book_by_id.return_value = {
                "id": "bl-1",
                "alternativeFormats": [
                    {"bookType": "AUDIOBOOK", "fileName": "track_a.m4b", "filePath": str(existing)},
                    {"bookType": "AUDIOBOOK", "fileName": "track_b.m4b", "filePath": "/missing/track_b.m4b"},
                ],
            }
            self.mock_booklore.download_audiobook_track.return_value = True

            result = self.service._copy_booklore_audio_files("bl-1", dest_folder, stage_mode="hardlink")

            self.assertTrue(result)
            self.assertEqual(self.mock_booklore.download_audiobook_track.call_count, 2)

    def test_booklore_single_stream_local_file_stages_as_track_000(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source = tmp_path / "single-stream.m4b"
            source.write_bytes(b"audio")
            dest_folder = tmp_path / "dest"
            self.service.ABS_AUDIO_ROOT = tmp_path
            self.mock_booklore.get_audiobook_info.return_value = {
                "chapters": [{"title": "Chapter 1"}],
                "mimeType": "audio/mp4",
            }
            self.mock_booklore.get_book_by_id.return_value = {
                "id": "bl-1",
                "alternativeFormats": [
                    {"bookType": "AUDIOBOOK", "fileName": source.name, "filePath": str(source)}
                ],
            }

            result = self.service._copy_booklore_audio_files("bl-1", dest_folder, stage_mode="cleanup")

            self.assertTrue(result)
            self.assertEqual((dest_folder / "track_000.m4b").read_bytes(), b"audio")
            self.mock_booklore.download_book_to_path.assert_not_called()

    def test_manual_forge_uses_booklore_audio_staging_for_booklore_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source_epub = tmp_path / "source.epub"
            source_epub.write_bytes(b"ebook")

            self.mock_storyteller.upload_epub.return_value = True
            self.mock_storyteller.upload_audio_file.return_value = True
            self.mock_storyteller.get_book_details.return_value = None

            with patch.object(self.service, "_copy_booklore_audio_files", return_value=False) as mock_booklore_copy, patch.object(
                self.service, "_copy_audio_files", return_value=True
            ) as mock_abs_copy, patch("src.services.forge_service.time.sleep", return_value=None):
                self.service._forge_background_task(
                    abs_id="booklore:42",
                    text_item={"source": "Local File", "path": str(source_epub)},
                    title="Auto Book",
                    author="Author",
                    audio_source="BookLore",
                    audio_source_id="42",
                )

            mock_booklore_copy.assert_called_once()
            mock_abs_copy.assert_not_called()

    def test_manual_forge_uploads_via_tus(self):
        """Manual forge should upload epub and audio files via TUS."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source_epub = tmp_path / "source.epub"
            source_epub.write_bytes(b"ebook")

            def _copy_audio(_abs_id, dest_path, stage_mode="cleanup"):
                dest = Path(dest_path)
                dest.mkdir(parents=True, exist_ok=True)
                (dest / "audio.mp3").write_bytes(b"audio")
                return True

            self.mock_storyteller.upload_epub.return_value = True
            self.mock_storyteller.upload_audio_file.return_value = True
            self.mock_storyteller.get_book_details.return_value = None

            with patch.object(self.service, "_copy_audio_files", side_effect=_copy_audio), patch(
                "src.services.forge_service.time.sleep", return_value=None
            ):
                self.service._forge_background_task(
                    abs_id="abs-1",
                    text_item={"source": "Local File", "path": str(source_epub)},
                    title="Auto Book",
                    author="Author",
                )

            self.mock_storyteller.upload_epub.assert_called_once()
            self.mock_storyteller.upload_audio_file.assert_called_once()

    def _write_storyteller_manifest(self, base_dir: Path) -> str:
        manifest_dir = base_dir / "storyteller_manifest"
        manifest_dir.mkdir(parents=True, exist_ok=True)
        chapter_file = manifest_dir / "00000-00001.json"
        chapter_payload = {
            "transcript": "hello world",
            "wordTimeline": [
                {
                    "startTime": 0.0,
                    "endTime": 0.5,
                    "startOffsetUtf16": 0,
                    "endOffsetUtf16": 5
                }
            ]
        }
        chapter_file.write_text(json.dumps(chapter_payload), encoding="utf-8")
        manifest = {
            "format": "storyteller_manifest",
            "duration": 10.0,
            "chapters": [
                {
                    "index": 0,
                    "file": chapter_file.name,
                    "start": 0.0,
                    "end": 10.0
                }
            ]
        }
        manifest_path = manifest_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        return str(manifest_path)

    def _run_auto_forge_pipeline(
        self,
        text_item: dict,
        stage_mode: str = "cleanup",
        ingest_manifest: str = None,
        storyteller_alignment_ok: bool = False,
        smil_transcript=None,
        whisper_transcript=None,
        audio_source: str = None,
        audio_source_id: str = None,
        staged_audio: bool = True,
        patch_ingest: bool = True,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            epub_cache_dir = tmp_path / "epub_cache"

            title = "Auto Book"

            source_epub = tmp_path / "source.epub"
            source_epub.write_bytes(b"source")
            if text_item.get("source") == "Local File" and not text_item.get("path"):
                text_item["path"] = str(source_epub)

            def _copy_audio(_abs_id, dest_path, stage_mode="cleanup"):
                dest = Path(dest_path)
                dest.mkdir(parents=True, exist_ok=True)
                if staged_audio:
                    (dest / "part_001.mp3").write_bytes(b"audio")
                return True

            self.service._copy_audio_files = MagicMock(side_effect=_copy_audio)
            self.mock_ebook_parser.epub_cache_dir = epub_cache_dir
            self.mock_ebook_parser.extract_text_and_map.return_value = ("full text", {})
            self.mock_alignment.align_storyteller_and_store.return_value = storyteller_alignment_ok
            self.mock_alignment.align_and_store.return_value = True
            if smil_transcript is None:
                self.mock_transcriber.transcribe_from_smil.return_value = [{"ts": 0.0, "char": 0}]
            else:
                self.mock_transcriber.transcribe_from_smil.return_value = smil_transcript
            self.mock_transcriber.process_audio.return_value = whisper_transcript

            self.mock_abs.get_item_details.return_value = {
                "media": {"chapters": [{"start": 0.0, "end": 5.0}]}
            }
            self.mock_abs.get_audio_files.return_value = [{"stream_url": "http://audio.test/1.mp3", "ext": "mp3"}]
            self.mock_abs.add_to_collection.return_value = True
            self.mock_booklore.add_to_shelf.return_value = True

            self.mock_storyteller.upload_epub.return_value = True
            self.mock_storyteller.upload_audio_file.return_value = True
            self.mock_storyteller.trigger_processing.return_value = True
            self.mock_storyteller.get_book_details.return_value = {
                "title": title,
                "ebook": {"filepath": "/storyteller/library/Auto Book/Auto Book.epub", "missing": 0},
                "audiobook": {"filepath": "/storyteller/library/Auto Book", "missing": 0},
                "readaloud": {
                    "status": "ALIGNED",
                    "currentStage": "SYNC_CHAPTERS",
                    "stageProgress": 1,
                },
                "alignedAt": "2026-03-25T17:28:56.000Z",
            }
            self.mock_storyteller.add_to_collection_by_uuid.return_value = True
            self.mock_storyteller.add_to_collection.return_value = True

            def _download_storyteller_book(_uuid, output_path, polling=False):
                output = Path(output_path)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(b"artifact")
                return True

            self.mock_storyteller.download_book.side_effect = _download_storyteller_book

            db_book = MagicMock()
            self.mock_db.get_book.return_value = db_book
            self.mock_db.save_book.return_value = db_book

            with ExitStack() as stack:
                stack.enter_context(
                    patch.dict(
                        os.environ,
                        {
                            "ABS_COLLECTION_NAME": "Synced with KOReader",
                            "BOOKLORE_SHELF_NAME": "Kobo",
                        },
                        clear=False,
                    )
                )
                stack.enter_context(patch("src.services.forge_service.time.sleep", return_value=None))
                if patch_ingest:
                    stack.enter_context(
                        patch(
                            "src.services.forge_service.ingest_storyteller_transcripts",
                            return_value=ingest_manifest,
                        )
                    )
                stack.enter_context(
                    patch(
                        "src.services.forge_service.probe_storyteller_transcripts",
                        return_value={"ready": True, "reason": "assets_not_configured"},
                    )
                )
                self.service._auto_forge_background_task(
                    abs_id="abs-1",
                    text_item=text_item,
                    title=title,
                    author="Auto Author",
                    original_filename="orig.epub",
                    original_hash="hash123",
                    audio_source=audio_source,
                    audio_source_id=audio_source_id,
                    stage_mode=stage_mode,
                )

            return db_book

    def test_auto_forge_cwa_falls_back_to_cwa_id_lookup(self):
        """Auto-forge should use CWA ID lookup when no direct download URL is provided."""
        def _download_cwa(url, output_path):
            Path(output_path).write_bytes(b"source")
            return True

        self.mock_cwa.download_ebook.side_effect = _download_cwa
        self.mock_cwa.get_book_by_id.return_value = {"download_url": "http://example.test/book.epub"}

        self._run_auto_forge_pipeline(
            text_item={"source": "CWA", "cwa_id": "123", "download_url": ""},
            ingest_manifest=None,
            storyteller_alignment_ok=False,
        )

        self.mock_cwa.get_book_by_id.assert_called_once_with("123")
        self.mock_cwa.download_ebook.assert_any_call("http://example.test/book.epub", ANY)

    def test_auto_forge_uses_storyteller_uuid_collection_path(self):
        """Auto-forge should add Storyteller books to collection by UUID when available."""
        self._run_auto_forge_pipeline(
            text_item={"source": "Local File"},
            ingest_manifest=None,
            storyteller_alignment_ok=False,
        )

        self.mock_storyteller.add_to_collection_by_uuid.assert_called_once()
        self.mock_storyteller.add_to_collection.assert_not_called()

    def test_auto_forge_uses_storyteller_alignment_before_smil(self):
        """Storyteller transcript alignment should run first; SMIL is fallback only."""
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = self._write_storyteller_manifest(Path(tmp))
            with patch("src.services.forge_service.ingest_storyteller_transcripts", return_value=manifest_path) as mock_ingest:
                self._run_auto_forge_pipeline(
                    text_item={"source": "Local File"},
                    ingest_manifest=manifest_path,
                    storyteller_alignment_ok=True,
                    patch_ingest=False,
                )

        self.mock_alignment.align_storyteller_and_store.assert_called_once()
        self.mock_transcriber.transcribe_from_smil.assert_not_called()
        self.mock_alignment.align_and_store.assert_not_called()
        self.assertEqual(mock_ingest.call_args.kwargs["storyteller_title"], "Auto Book")

    def test_auto_forge_falls_back_to_whisper_when_smil_rejected(self):
        """Auto-forge should run Whisper fallback if SMIL returns no transcript."""
        self._run_auto_forge_pipeline(
            text_item={"source": "Local File"},
            ingest_manifest=None,
            storyteller_alignment_ok=False,
            smil_transcript=[],
            whisper_transcript=[{"start": 0.0, "end": 1.0, "text": "hello"}],
        )

        self.mock_abs.get_audio_files.assert_not_called()
        self.mock_transcriber.process_audio.assert_called_once()
        self.mock_alignment.align_and_store.assert_called_once()

    def test_auto_forge_uses_smil_and_skips_whisper_when_smil_available(self):
        """Auto-forge should not call Whisper when SMIL transcript is valid."""
        self._run_auto_forge_pipeline(
            text_item={"source": "Local File"},
            ingest_manifest=None,
            storyteller_alignment_ok=False,
            smil_transcript=[{"start": 0.0, "end": 1.0, "text": "from smil"}],
            whisper_transcript=[{"start": 0.0, "end": 1.0, "text": "from whisper"}],
        )

        self.mock_transcriber.process_audio.assert_not_called()
        self.mock_alignment.align_and_store.assert_called_once()

    def test_auto_forge_sets_error_status_on_pipeline_failure(self):
        """Auto-forge should set book status to error when pipeline fails."""
        db_book = self._run_auto_forge_pipeline(
            text_item={"source": "Local File"},
            ingest_manifest=None,
            storyteller_alignment_ok=False,
            smil_transcript=[],
            whisper_transcript=[],
        )

        self.assertEqual(db_book.status, "error")

    def test_auto_forge_booklore_hardlink_mode_forwards_to_booklore_staging(self):
        with patch.object(self.service, "_copy_booklore_audio_files", return_value=True) as mock_copy_booklore, patch.object(
            self.service, "_copy_audio_files", return_value=True
        ) as mock_copy_abs:
            self._run_auto_forge_pipeline(
                text_item={"source": "Local File"},
                stage_mode="hardlink",
                ingest_manifest=None,
                storyteller_alignment_ok=False,
                audio_source="BookLore",
                audio_source_id="bl-1",
            )

        mock_copy_booklore.assert_called_once()
        self.assertEqual(mock_copy_booklore.call_args.args[0], "bl-1")
        self.assertEqual(mock_copy_booklore.call_args.kwargs["stage_mode"], "hardlink")
        mock_copy_abs.assert_not_called()

    def test_poll_auto_forge_completion_api_metadata_does_not_mark_complete(self):
        """Metadata readiness alone should not mark auto-forge complete."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            epub_cache = tmp_path / "epub_cache"
            epub_cache.mkdir(parents=True, exist_ok=True)

            st_client = MagicMock()
            st_client.get_book_details.return_value = {
                "readaloud": {"filepath": "/storyteller/output/readaloud.epub"}
            }

            with patch(
                "src.services.forge_service.probe_storyteller_transcripts",
                return_value={"ready": True, "reason": "assets_not_configured"},
            ):
                result = self.service._poll_auto_forge_completion(
                    st_client=st_client,
                    book_uuid="uuid-1",
                    title="Auto Book",
                    chapters=[],
                    epub_cache=epub_cache,
                    processing_triggered=True,
                    poll_count=1,
                )

            self.assertIsNone(result["completion_method"])
            self.assertIsNone(result["readaloud_status"])
            self.assertFalse(result["terminal_error"])
            st_client.download_book.assert_not_called()

    def test_poll_auto_forge_completion_does_not_trigger_before_processing_ready(self):
        """Auto-forge should delay /process until Storyteller exposes linked ebook and audiobook."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            epub_cache = tmp_path / "epub_cache"
            epub_cache.mkdir(parents=True, exist_ok=True)

            st_client = MagicMock()
            st_client.get_book_details.return_value = {
                "ebook": {"filepath": "/storyteller/library/Auto Book/Auto Book.epub", "missing": 0}
            }

            with patch(
                "src.services.forge_service.probe_storyteller_transcripts",
                return_value={"ready": True, "reason": "assets_not_configured"},
            ):
                result = self.service._poll_auto_forge_completion(
                    st_client=st_client,
                    book_uuid="uuid-1",
                    title="Auto Book",
                    chapters=[],
                    epub_cache=epub_cache,
                    processing_triggered=False,
                    poll_count=4,
                )

            st_client.trigger_processing.assert_not_called()
            self.assertFalse(result["processing_triggered"])

    def test_poll_auto_forge_completion_triggers_when_processing_ready(self):
        """Auto-forge should trigger processing once Storyteller reports both links ready."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            epub_cache = tmp_path / "epub_cache"
            epub_cache.mkdir(parents=True, exist_ok=True)

            st_client = MagicMock()
            st_client.get_book_details.return_value = {
                "ebook": {"filepath": "/storyteller/library/Auto Book/Auto Book.epub", "missing": 0},
                "audiobook": {"filepath": "/storyteller/library/Auto Book", "missing": 0},
            }

            with patch(
                "src.services.forge_service.probe_storyteller_transcripts",
                return_value={"ready": True, "reason": "assets_not_configured"},
            ):
                result = self.service._poll_auto_forge_completion(
                    st_client=st_client,
                    book_uuid="uuid-1",
                    title="Auto Book",
                    chapters=[],
                    epub_cache=epub_cache,
                    processing_triggered=False,
                    poll_count=4,
                )

            st_client.trigger_processing.assert_called_once_with("uuid-1")
            self.assertTrue(result["processing_triggered"])

    def test_poll_auto_forge_completion_marks_complete_when_aligned_even_if_transcripts_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            epub_cache = tmp_path / "epub_cache"
            epub_cache.mkdir(parents=True, exist_ok=True)

            st_client = MagicMock()
            st_client.get_book_details.return_value = {
                "title": "Auto Book",
                "ebook": {"filepath": "/storyteller/library/Auto Book/Auto Book.epub", "missing": 0},
                "audiobook": {"filepath": "/storyteller/library/Auto Book", "missing": 0},
                "readaloud": {
                    "filepath": "/storyteller/library/Auto Book/Auto Book (readaloud).epub",
                    "status": "ALIGNED",
                    "currentStage": "SYNC_CHAPTERS",
                    "stageProgress": 1,
                },
                "alignedAt": "2026-03-25T17:28:56.000Z",
            }

            with patch(
                "src.services.forge_service.probe_storyteller_transcripts",
                return_value={"ready": False, "reason": "chapter_set_incomplete"},
            ):
                result = self.service._poll_auto_forge_completion(
                    st_client=st_client,
                    book_uuid="uuid-1",
                    title="Auto Book",
                    chapters=[],
                    epub_cache=epub_cache,
                    processing_triggered=True,
                    poll_count=4,
                )

            self.assertEqual(result["completion_method"], "storyteller_aligned")
            self.assertEqual(result["readaloud_status"], "ALIGNED")
            self.assertEqual(result["aligned_at"], "2026-03-25T17:28:56.000Z")
            self.assertFalse(result["terminal_error"])
            st_client.download_book.assert_not_called()

    def test_poll_auto_forge_completion_requires_aligned_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            epub_cache = tmp_path / "epub_cache"
            epub_cache.mkdir(parents=True, exist_ok=True)

            st_client = MagicMock()
            st_client.get_book_details.return_value = {
                "ebook": {"filepath": "/storyteller/library/Auto Book/Auto Book.epub", "missing": 0},
                "audiobook": {"filepath": "/storyteller/library/Auto Book", "missing": 0},
                "readaloud": {
                    "status": "ALIGNED",
                    "currentStage": "SYNC_CHAPTERS",
                    "stageProgress": 1,
                },
            }

            with patch(
                "src.services.forge_service.probe_storyteller_transcripts",
                return_value={"ready": True, "reason": "validated"},
            ):
                result = self.service._poll_auto_forge_completion(
                    st_client=st_client,
                    book_uuid="uuid-1",
                    title="Auto Book",
                    chapters=[],
                    epub_cache=epub_cache,
                    processing_triggered=True,
                    poll_count=4,
                )

            self.assertIsNone(result["completion_method"])
            self.assertEqual(result["readaloud_status"], "ALIGNED")
            self.assertIsNone(result["aligned_at"])
            self.assertFalse(result["terminal_error"])
            st_client.download_book.assert_not_called()

    def test_poll_auto_forge_completion_error_returns_terminal_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            epub_cache = tmp_path / "epub_cache"
            epub_cache.mkdir(parents=True, exist_ok=True)

            st_client = MagicMock()
            st_client.get_book_details.return_value = {
                "ebook": {"filepath": "/storyteller/library/Auto Book/Auto Book.epub", "missing": 0},
                "audiobook": {"filepath": "/storyteller/library/Auto Book", "missing": 0},
                "readaloud": {
                    "status": "ERROR",
                    "currentStage": "SYNC_CHAPTERS",
                    "stageProgress": 1.25,
                },
            }

            with patch(
                "src.services.forge_service.probe_storyteller_transcripts",
                return_value={"ready": False, "reason": "chapter_set_incomplete"},
            ):
                result = self.service._poll_auto_forge_completion(
                    st_client=st_client,
                    book_uuid="uuid-1",
                    title="Auto Book",
                    chapters=[],
                    epub_cache=epub_cache,
                    processing_triggered=True,
                    poll_count=4,
                )

            self.assertIsNone(result["completion_method"])
            self.assertEqual(result["readaloud_status"], "ERROR")
            self.assertTrue(result["terminal_error"])
            self.assertEqual(result["terminal_error_reason"], "readaloud_error")
            st_client.download_book.assert_not_called()

    def test_poll_auto_forge_completion_non_aligned_status_does_not_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            epub_cache = tmp_path / "epub_cache"
            epub_cache.mkdir(parents=True, exist_ok=True)

            st_client = MagicMock()
            st_client.get_book_details.return_value = {
                "ebook": {"filepath": "/storyteller/library/Auto Book/Auto Book.epub", "missing": 0},
                "audiobook": {"filepath": "/storyteller/library/Auto Book", "missing": 0},
                "readaloud": {
                    "status": "PROCESSING",
                    "currentStage": "SYNC_CHAPTERS",
                    "stageProgress": 0.87,
                },
                "alignedAt": "2026-03-25T17:28:56.000Z",
            }

            with patch(
                "src.services.forge_service.probe_storyteller_transcripts",
                return_value={"ready": False, "reason": "chapter_set_incomplete"},
            ):
                result = self.service._poll_auto_forge_completion(
                    st_client=st_client,
                    book_uuid="uuid-1",
                    title="Auto Book",
                    chapters=[],
                    epub_cache=epub_cache,
                    processing_triggered=True,
                    poll_count=4,
                )

            self.assertIsNone(result["completion_method"])
            self.assertEqual(result["readaloud_status"], "PROCESSING")
            self.assertFalse(result["terminal_error"])
            st_client.download_book.assert_not_called()

    def test_auto_forge_waits_for_storyteller_processing_readiness_before_trigger(self):
        """Auto-forge should poll readiness instead of triggering immediately on UUID discovery."""
        responses = [
            None,
            {
                "ebook": {"filepath": "/storyteller/library/Auto Book/Auto Book.epub", "missing": 0}
            },
            {
                "title": "Auto Book",
                "ebook": {"filepath": "/storyteller/library/Auto Book/Auto Book.epub", "missing": 0},
                "audiobook": {"filepath": "/storyteller/library/Auto Book", "missing": 0},
                "readaloud": {
                    "status": "ALIGNED",
                    "currentStage": "SYNC_CHAPTERS",
                    "stageProgress": 1,
                },
                "alignedAt": "2026-03-25T17:28:56.000Z",
            },
        ]

        def _next_details(*_args, **_kwargs):
            if len(responses) > 1:
                return responses.pop(0)
            return responses[0]

        self.mock_storyteller.get_book_details.side_effect = _next_details

        self._run_auto_forge_pipeline(
            text_item={"source": "Local File"},
            ingest_manifest=None,
            storyteller_alignment_ok=False,
        )

        self.assertGreaterEqual(self.mock_storyteller.get_book_details.call_count, 3)
        self.mock_storyteller.trigger_processing.assert_called_once()

    def test_auto_forge_uploads_epub_and_audio_via_tus(self):
        """Auto-forge should call upload_epub and upload_audio_file via TUS."""
        self._run_auto_forge_pipeline(
            text_item={"source": "Local File"},
            ingest_manifest=None,
            storyteller_alignment_ok=False,
        )

        self.mock_storyteller.upload_epub.assert_called_once()
        self.mock_storyteller.upload_audio_file.assert_called_once()

    def test_auto_forge_times_out_when_completion_never_confirmed(self):
        """Auto-forge should enter recovery and eventually time out if completion is never confirmed."""
        with patch.object(
            self.service,
            "_poll_auto_forge_completion",
            return_value={
                "processing_triggered": True,
                "completion_method": None,
                "transcript_probe": {"ready": False, "reason": "chapter_set_incomplete"},
                "readaloud_status": "PROCESSING",
                "aligned_at": None,
                "terminal_error": False,
                "terminal_error_reason": None,
                "storyteller_title": "Auto Book",
            },
        ), patch.dict(
            os.environ,
            {"STORYTELLER_RECOVERY_MAX_WAIT_SECONDS": "0"},
            clear=False,
        ):
            self.service.storyteller_recovery_max_wait_seconds = 0
            db_book = self._run_auto_forge_pipeline(
                text_item={"source": "Local File"},
                ingest_manifest=None,
                storyteller_alignment_ok=False,
                smil_transcript=[],
                whisper_transcript=[],
            )

        self.assertEqual(db_book.status, "forging")

    def test_auto_forge_fails_fast_when_storyteller_reports_error(self):
        with patch.object(
            self.service,
            "_poll_auto_forge_completion",
            return_value={
                "processing_triggered": True,
                "completion_method": None,
                "transcript_probe": {"ready": False, "reason": "chapter_set_incomplete"},
                "readaloud_status": "ERROR",
                "aligned_at": None,
                "terminal_error": True,
                "terminal_error_reason": "readaloud_error",
                "storyteller_title": "Auto Book",
            },
        ):
            db_book = self._run_auto_forge_pipeline(
                text_item={"source": "Local File"},
                ingest_manifest=None,
                storyteller_alignment_ok=False,
            )

        self.assertEqual(db_book.status, "error")
        self.mock_storyteller.download_book.assert_not_called()

    def test_auto_forge_whisper_fallback_prefers_staged_audio(self):
        self._run_auto_forge_pipeline(
            text_item={"source": "Local File"},
            ingest_manifest=None,
            storyteller_alignment_ok=False,
            smil_transcript=[],
            whisper_transcript=[{"start": 0.0, "end": 1.0, "text": "hello"}],
        )

        audio_inputs = self.mock_transcriber.process_audio.call_args.args[1]
        self.assertTrue(audio_inputs)
        self.assertIn("local_path", audio_inputs[0])
        self.mock_abs.get_audio_files.assert_not_called()

    def test_auto_forge_whisper_fallback_uses_booklore_source_for_booklore_jobs(self):
        def _copy_booklore(_book_id, dest_path, stage_mode="cleanup"):
            dest = Path(dest_path)
            dest.mkdir(parents=True, exist_ok=True)
            if "whisper_source_audio" in str(dest):
                (dest / "part_001.m4b").write_bytes(b"audio")
            return True

        with patch.object(self.service, "_copy_booklore_audio_files", side_effect=_copy_booklore):
            self._run_auto_forge_pipeline(
                text_item={"source": "Local File"},
                ingest_manifest=None,
                storyteller_alignment_ok=False,
                smil_transcript=[],
                whisper_transcript=[{"start": 0.0, "end": 1.0, "text": "hello"}],
                audio_source="BookLore",
                audio_source_id="bl-1",
                staged_audio=False,
            )

        audio_inputs = self.mock_transcriber.process_audio.call_args.args[1]
        self.assertTrue(audio_inputs)
        self.assertIn("local_path", audio_inputs[0])
        self.mock_abs.get_audio_files.assert_not_called()

    def test_auto_forge_whisper_fallback_uses_abs_audio_for_abs_jobs(self):
        self.mock_abs.get_audio_files.return_value = [{"stream_url": "http://audio.test/1.mp3", "ext": "mp3"}]

        self._run_auto_forge_pipeline(
            text_item={"source": "Local File"},
            ingest_manifest=None,
            storyteller_alignment_ok=False,
            smil_transcript=[],
            whisper_transcript=[{"start": 0.0, "end": 1.0, "text": "hello"}],
            staged_audio=False,
        )

        self.mock_abs.get_audio_files.assert_called_once_with("abs-1")


if __name__ == '__main__':
    unittest.main()
