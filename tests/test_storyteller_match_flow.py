import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


class _MatchFlowContainer:
    def __init__(self):
        self._sync_manager = Mock()
        self._sync_manager.get_abs_title.return_value = "Delta V"
        self._sync_manager.get_duration.return_value = 3600.0

        self._abs_client = Mock()
        self._booklore_client = Mock()
        self._storyteller_client = Mock()
        self._storygraph_client = Mock()
        self._database_service = Mock()
        self._database_service.get_all_settings.return_value = {}

        self._sync_clients = {
            "Hardcover": Mock(is_configured=Mock(return_value=False)),
            "StoryGraph": Mock(is_configured=Mock(return_value=False)),
        }
        self._ebook_parser = Mock()
        self._forge_service = Mock(active_tasks=set())

    def sync_manager(self):
        return self._sync_manager

    def abs_client(self):
        return self._abs_client

    def booklore_client(self):
        return self._booklore_client

    def storyteller_client(self):
        return self._storyteller_client

    def storygraph_client(self):
        return self._storygraph_client

    def database_service(self):
        return self._database_service

    def sync_clients(self):
        return self._sync_clients

    def ebook_parser(self):
        return self._ebook_parser

    def forge_service(self):
        return self._forge_service

    def data_dir(self):
        return Path(tempfile.gettempdir())

    def books_dir(self):
        return Path(tempfile.gettempdir())

    def epub_cache_dir(self):
        return Path(tempfile.gettempdir()) / "test_epub_cache"


class TestStorytellerMatchFlow(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.assets_root = Path(self.temp_dir) / "storyteller_assets"
        self.data_dir = Path(self.temp_dir) / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)

        os.environ["DATA_DIR"] = str(self.data_dir)
        os.environ["BOOKS_DIR"] = self.temp_dir
        os.environ["STORYTELLER_ASSETS_DIR"] = str(self.assets_root)

        transcriptions_dir = self.assets_root / "assets" / "Delta V" / "transcriptions"
        transcriptions_dir.mkdir(parents=True, exist_ok=True)
        chapter_payload = {
            "transcript": "hello world",
            "wordTimeline": [
                {
                    "type": "word",
                    "text": "hello",
                    "startTime": 0.5,
                    "endTime": 1.0,
                    "startOffsetUtf16": 0,
                    "endOffsetUtf16": 5,
                    "timeline": [],
                }
            ],
        }
        (transcriptions_dir / "00001-00001.json").write_text(
            json.dumps(chapter_payload), encoding="utf-8"
        )

        self.container = _MatchFlowContainer()

        def mock_initialize_database(_):
            return self.container._database_service

        import src.db.migration_utils
        self._original_initialize_database = src.db.migration_utils.initialize_database
        src.db.migration_utils.initialize_database = mock_initialize_database

        from src.web_server import create_app

        self.app, _ = create_app(test_container=self.container)
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

        self.container._abs_client.get_all_audiobooks.return_value = [
            {"id": "abs-1", "media": {"metadata": {"title": "Delta V"}, "duration": 3600.0}}
        ]
        self.container._abs_client.get_item_details.return_value = {
            "media": {"chapters": [{"start": 0.0, "end": 10.0}]}
        }
        self.container._abs_client.add_to_collection.return_value = True
        self.container._booklore_client.is_configured.return_value = False
        self.container._storyteller_client.is_configured.return_value = False

        self.container._database_service.get_book_by_kosync_id.return_value = None
        self.container._database_service.get_book.return_value = None

    def tearDown(self):
        import shutil
        import src.db.migration_utils

        src.db.migration_utils.initialize_database = self._original_initialize_database
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        os.environ.pop("STORYTELLER_ASSETS_DIR", None)

    def test_match_route_ingests_storyteller_transcript_and_sets_source(self):
        import src.web_server

        original_hash_lookup = src.web_server.get_kosync_id_for_ebook
        src.web_server.get_kosync_id_for_ebook = Mock(return_value="hash-1")

        try:
            response = self.client.post(
                "/match",
                data={"audiobook_id": "abs-1", "ebook_filename": "delta-v.epub"},
            )
        finally:
            src.web_server.get_kosync_id_for_ebook = original_hash_lookup

        self.assertEqual(response.status_code, 302)
        self.container._database_service.save_book.assert_called_once()

        saved_book = self.container._database_service.save_book.call_args[0][0]
        self.assertEqual(saved_book.transcript_source, "storyteller")
        self.assertIsNotNone(saved_book.transcript_file)

        manifest_path = Path(saved_book.transcript_file)
        self.assertTrue(manifest_path.exists())

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["format"], "storyteller_manifest")
        self.assertEqual(manifest["chapter_count"], 1)
        self.assertTrue((manifest_path.parent / "00000-00001.json").exists())

    @patch("src.web_server.ingest_storyteller_transcripts", return_value=None)
    @patch("src.web_server.get_kosync_id_for_ebook", return_value="hash-story-uuid-1")
    def test_match_route_preserves_storyteller_source_when_ingest_missing(self, _mock_kosync, _mock_ingest):
        self.container._storyteller_client.download_book.return_value = True

        response = self.client.post(
            "/match",
            data={
                "audiobook_id": "abs-1",
                "ebook_filename": "delta-v.epub",
                "storyteller_uuid": "story-uuid-1",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.container._database_service.save_book.assert_called_once()

        saved_book = self.container._database_service.save_book.call_args[0][0]
        self.assertEqual(saved_book.storyteller_uuid, "story-uuid-1")
        self.assertEqual(saved_book.transcript_source, "storyteller")
        self.assertIsNone(saved_book.transcript_file)
