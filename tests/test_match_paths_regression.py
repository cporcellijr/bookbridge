import os
import sys
import tempfile
import unittest
import json
import time
from pathlib import Path
from unittest.mock import Mock, patch

# Add project root to Python path
sys.path.insert(0, str(Path(__file__).parent.parent))

import src.web_server as web_server


class MockContainer:
    """Mock container implementing the web dependency contract."""

    def __init__(self):
        self.mock_sync_manager = Mock()
        self.mock_abs_client = Mock()
        self.mock_booklore_client = Mock()
        self.mock_bookorbit_client = Mock()
        self.mock_storyteller_client = Mock()
        self.mock_storygraph_client = Mock()
        self.mock_database_service = Mock()
        self.mock_ebook_parser = Mock()
        self.mock_forge_service = Mock()

        # Default DB behavior
        self.mock_database_service.get_all_settings.return_value = {}
        self.mock_database_service.get_all_books.return_value = []
        self.mock_database_service.get_all_storygraph_details.return_value = []
        self.mock_database_service.get_all_pending_suggestions.return_value = []
        self.mock_database_service.get_ignored_suggestion_source_ids.return_value = []
        self.mock_database_service.get_kosync_doc_by_filename.return_value = None
        self.mock_database_service.ignore_suggestion.return_value = True
        self.mock_database_service.get_book.return_value = None
        self.mock_database_service.get_book_by_kosync_id.return_value = None
        self.mock_database_service.get_pending_suggestion.return_value = None

        # Default manager behavior
        self.mock_sync_manager.abs_client = self.mock_abs_client
        self.mock_sync_manager.get_abs_title.return_value = "Regression Book"
        self.mock_sync_manager.get_duration.return_value = 3600

        # Default ABS behavior
        self.mock_abs_client.base_url = "http://abs.test"
        self.mock_abs_client.token = "token"
        self.mock_abs_client.get_all_audiobooks.return_value = [
            {
                "id": "ab-1",
                "media": {
                    "metadata": {"title": "Regression Book", "authorName": "Test Author"},
                    "duration": 3600,
                },
            }
        ]
        self.mock_abs_client.get_item_details.return_value = {
            "media": {
                "chapters": [{"start": 0.0, "end": 10.0}],
                "metadata": {"title": "Regression Book", "authorName": "Test Author"},
            }
        }

        # Default booklore behavior
        self.mock_booklore_client.is_configured.return_value = True
        self.mock_booklore_client.find_book_by_filename.return_value = {"id": "bl-1"}
        self.mock_bookorbit_client.is_configured.return_value = True
        self.mock_bookorbit_client.remove_from_shelf.return_value = True
        self.mock_bookorbit_client.move_between_shelves.return_value = True

        # Default storyteller behavior
        self.mock_storyteller_client.is_configured.return_value = False

        # Default sync clients map
        self._sync_clients = {
            "Hardcover": Mock(is_configured=Mock(return_value=False)),
            "StoryGraph": Mock(is_configured=Mock(return_value=False)),
        }

    def sync_manager(self):
        return self.mock_sync_manager

    def abs_client(self):
        return self.mock_abs_client

    def booklore_client(self):
        return self.mock_booklore_client

    def bookorbit_client(self):
        return self.mock_bookorbit_client

    def storyteller_client(self):
        return self.mock_storyteller_client

    def storygraph_client(self):
        return self.mock_storygraph_client

    def ebook_parser(self):
        return self.mock_ebook_parser

    def forge_service(self):
        return self.mock_forge_service

    def database_service(self):
        return self.mock_database_service

    def sync_clients(self):
        return self._sync_clients

    def data_dir(self):
        return Path(tempfile.gettempdir()) / "test_data_match_paths"

    def books_dir(self):
        return Path(tempfile.gettempdir()) / "test_books_match_paths"

    def epub_cache_dir(self):
        return Path(tempfile.gettempdir()) / "test_epub_cache_match_paths"


class TestMatchPathsRegression(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        os.environ["DATA_DIR"] = self.temp_dir
        os.environ["BOOKS_DIR"] = self.temp_dir
        # Point the app at the real templates dir so XHR fragment responses render.
        os.environ["TEMPLATE_DIR"] = str(Path(__file__).parent.parent / "templates")

        self.mock_container = MockContainer()

        def _mock_initialize_database(_data_dir):
            return self.mock_container.mock_database_service

        import src.db.migration_utils

        self.original_init_db = src.db.migration_utils.initialize_database
        src.db.migration_utils.initialize_database = _mock_initialize_database

        from src.web_server import create_app
        import src.web_server as web_server

        # Ensure isolated in-memory scan state per test run
        with web_server.SUGGESTIONS_SCAN_JOBS_LOCK:
            web_server.SUGGESTIONS_SCAN_JOBS.clear()
        with web_server.SUGGESTIONS_STATE_LOCK:
            web_server.SUGGESTIONS_STATE_STORE.clear()

        self.app, _ = create_app(test_container=self.mock_container)
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

        # The batch-match queue is now a server-side file (DATA_DIR/match_queue.json),
        # not the per-client session cookie — reset it so tests don't leak into each other.
        web_server._match_queue_clear()

    def tearDown(self):
        import shutil
        import src.db.migration_utils

        src.db.migration_utils.initialize_database = self.original_init_db
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _prepare_storyteller_assets(self, title: str, chapter_count: int = 2):
        assets_root = Path(self.temp_dir) / "storyteller_assets"
        transcriptions_dir = assets_root / "assets" / title / "transcriptions"
        transcriptions_dir.mkdir(parents=True, exist_ok=True)
        for idx in range(chapter_count):
            chapter_name = f"{idx + 1:05d}-00001.json"
            payload = {"transcript": f"chapter {idx + 1}", "wordTimeline": []}
            (transcriptions_dir / chapter_name).write_text(json.dumps(payload), encoding="utf-8")
        os.environ["STORYTELLER_ASSETS_DIR"] = str(assets_root)
        self.addCleanup(lambda: os.environ.pop("STORYTELLER_ASSETS_DIR", None))

    def _set_abs_chapters(self, chapter_count: int = 2):
        chapters = [{"start": idx * 10.0, "end": (idx + 1) * 10.0} for idx in range(chapter_count)]
        self.mock_container.mock_abs_client.get_item_details.return_value = {
            "media": {
                "chapters": chapters,
                "metadata": {"title": "Regression Book", "authorName": "Test Author"},
            }
        }

    def _post_as_user(self, user_id, path, data):
        token = web_server.set_current_user_id(user_id)
        try:
            return self.client.post(path, data=data)
        finally:
            web_server.reset_current_user_id(token)

    def _load_queue_as_user(self, user_id):
        token = web_server.set_current_user_id(user_id)
        try:
            return web_server._load_match_queue()
        finally:
            web_server.reset_current_user_id(token)

    @patch("src.web_server.get_kosync_id_for_ebook", return_value="hash-match-1")
    def test_match_route_creates_mapping(self, _mock_kosync):
        response = self.client.post(
            "/match",
            data={
                "audiobook_id": "ab-1",
                "ebook_filename": "book.epub",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("/"))

        self.mock_container.mock_database_service.save_book.assert_called_once()
        saved_book = self.mock_container.mock_database_service.save_book.call_args[0][0]
        self.assertEqual(saved_book.abs_id, "ab-1")
        self.assertEqual(saved_book.ebook_filename, "book.epub")
        self.assertEqual(saved_book.kosync_doc_id, "hash-match-1")
        self.assertEqual(saved_book.status, "pending")

        self.mock_container.mock_database_service.dismiss_suggestion.assert_any_call("ab-1")
        self.mock_container.mock_database_service.dismiss_suggestion.assert_any_call("hash-match-1")
        self.mock_container.mock_abs_client.add_to_collection.assert_called_once_with("ab-1", "Synced with KOReader")
        self.mock_container.mock_booklore_client.add_to_shelf.assert_called_once_with("book.epub", "Kobo")

    @patch("src.web_server.get_kosync_id_for_ebook", return_value="1234567890abcdef1234567890abcdef")
    def test_match_route_creates_ebook_only_mapping_from_storyteller_without_audiobook(self, _mock_kosync):
        self.mock_container.mock_storyteller_client.download_book.return_value = True
        self.mock_container.mock_storyteller_client.is_configured.return_value = True
        self.mock_container.mock_storyteller_client.get_book_details.return_value = {
            "title": "Story Only Title",
            "subtitle": "Story Only Subtitle",
            "authors": [{"name": "Story Only Author"}],
        }

        response = self.client.post(
            "/match",
            data={
                "storyteller_uuid": "story-uuid-ebook-only",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.mock_container.mock_database_service.save_book.assert_called_once()
        saved_book = self.mock_container.mock_database_service.save_book.call_args[0][0]
        self.assertEqual(saved_book.abs_id, "ebook-1234567890abcdef")
        self.assertEqual(saved_book.abs_title, "Story Only Title")
        self.assertEqual(saved_book.sync_mode, "ebook_only")
        self.assertEqual(saved_book.storyteller_uuid, "story-uuid-ebook-only")
        self.assertEqual(saved_book.transcript_source, "storyteller")
        self.mock_container.mock_abs_client.add_to_collection.assert_not_called()

    @patch("src.web_server.get_kosync_id_for_ebook", return_value="abcdef1234567890abcdef1234567890")
    def test_match_route_ebook_only_storyteller_preserves_original_filename_for_hash(self, _mock_kosync):
        self.mock_container.mock_storyteller_client.download_book.return_value = True
        self.mock_container.mock_booklore_client.find_book_by_filename.return_value = None

        response = self.client.post(
            "/match",
            data={
                "ebook_filename": "ebook-source.epub",
                "storyteller_uuid": "story-uuid-with-original",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.mock_container.mock_database_service.save_book.assert_called_once()
        saved_book = self.mock_container.mock_database_service.save_book.call_args[0][0]
        self.assertEqual(saved_book.sync_mode, "ebook_only")
        self.assertEqual(saved_book.original_ebook_filename, "ebook-source.epub")
        self.assertEqual(saved_book.ebook_filename, "storyteller_story-uuid-with-original.epub")
        self.assertEqual(saved_book.kosync_doc_id, "abcdef1234567890abcdef1234567890")

    def test_match_route_rejects_ebook_only_without_text_source(self):
        response = self.client.post("/match", data={})
        self.assertEqual(response.status_code, 400)
        self.assertIn("Please select a text source", response.get_data(as_text=True))

    @patch("src.web_server.get_kosync_id_for_ebook", return_value="hash-match-story-real")
    def test_match_storyteller_uuid_real_ingest_persists_manifest(self, _mock_kosync):
        self._prepare_storyteller_assets("Regression Book", chapter_count=2)
        self._set_abs_chapters(chapter_count=2)
        self.mock_container.mock_storyteller_client.download_book.return_value = True

        response = self.client.post(
            "/match",
            data={
                "audiobook_id": "ab-1",
                "ebook_filename": "book.epub",
                "storyteller_uuid": "story-uuid-match-real",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.mock_container.mock_database_service.save_book.assert_called_once()
        saved_book = self.mock_container.mock_database_service.save_book.call_args[0][0]
        self.assertEqual(saved_book.storyteller_uuid, "story-uuid-match-real")
        self.assertEqual(saved_book.transcript_source, "storyteller")
        self.assertIsNotNone(saved_book.transcript_file)
        self.assertTrue(Path(saved_book.transcript_file).exists())

    @patch("src.web_server.ingest_storyteller_transcripts", return_value=None)
    @patch("src.web_server.get_kosync_id_for_ebook", side_effect=[None, "hash-match-story-fallback"])
    def test_match_storyteller_uuid_falls_back_to_artifact_hash_when_original_missing(self, _mock_kosync, _mock_ingest):
        self.mock_container.mock_storyteller_client.download_book.return_value = True
        self.mock_container.mock_booklore_client.find_book_by_filename.return_value = None

        response = self.client.post(
            "/match",
            data={
                "audiobook_id": "ab-1",
                "ebook_filename": "book.epub",
                "storyteller_uuid": "story-uuid-match-fallback",
            },
        )

        self.assertEqual(response.status_code, 302)
        saved_book = self.mock_container.mock_database_service.save_book.call_args[0][0]
        self.assertEqual(saved_book.kosync_doc_id, "hash-match-story-fallback")
        call_args = [call.args for call in _mock_kosync.call_args_list]
        self.assertEqual(call_args[0], ("book.epub", None))
        self.assertEqual(call_args[1], ("storyteller_story-uuid-match-fallback.epub",))

    @patch("src.web_server.get_kosync_id_for_ebook", return_value="hash-forge-1")
    def test_match_forge_action_only_stages(self, _mock_kosync):
        response = self.client.post(
            "/match",
            data={
                "action": "forge_match",
                "audiobook_id": "ab-1",
                "ebook_filename": "source.epub",
                "source_type": "Booklore",
                "source_id": "42",
                "source_path": "",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("/"))

        self.mock_container.mock_database_service.save_book.assert_called_once()
        staged_book = self.mock_container.mock_database_service.save_book.call_args[0][0]
        self.assertEqual(staged_book.abs_id, "ab-1")
        self.assertEqual(staged_book.ebook_filename, "source.epub")
        self.assertEqual(staged_book.kosync_doc_id, "hash-forge-1")
        self.assertEqual(staged_book.status, "forging")

        self.mock_container.mock_forge_service.start_auto_forge_match.assert_called_once()
        kwargs = self.mock_container.mock_forge_service.start_auto_forge_match.call_args.kwargs
        self.assertEqual(kwargs["abs_id"], "ab-1")
        self.assertEqual(kwargs["original_filename"], "source.epub")
        self.assertEqual(kwargs["original_hash"], "hash-forge-1")

        # Route should stage only; final linking side effects happen after forge completion.
        self.mock_container.mock_abs_client.add_to_collection.assert_not_called()
        self.mock_container.mock_booklore_client.add_to_shelf.assert_not_called()

    @patch("src.web_server.get_kosync_id_for_ebook", return_value="hash-forge-hardlink")
    def test_match_forge_action_forwards_stage_mode(self, _mock_kosync):
        response = self.client.post(
            "/match",
            data={
                "action": "forge_match",
                "audiobook_id": "ab-1",
                "ebook_filename": "source.epub",
                "source_type": "Booklore",
                "source_id": "42",
                "source_path": "",
                "forge_stage_mode": "hardlink",
            },
        )

        self.assertEqual(response.status_code, 302)
        kwargs = self.mock_container.mock_forge_service.start_auto_forge_match.call_args.kwargs
        self.assertEqual(kwargs["stage_mode"], "hardlink")

    def test_forge_process_forwards_stage_mode(self):
        response = self.client.post(
            "/api/forge/process",
            json={
                "abs_id": "ab-1",
                "text_item": {"source": "Booklore", "booklore_id": "42"},
                "forge_stage_mode": "hardlink",
            },
        )

        self.assertEqual(response.status_code, 202)
        self.mock_container.mock_forge_service.start_manual_forge.assert_called_once_with(
            "ab-1",
            {"source": "Booklore", "booklore_id": "42"},
            "Regression Book",
            "Test Author",
            stage_mode="hardlink",
        )

    def test_forge_search_audio_includes_booklore_results(self):
        self.mock_container.mock_booklore_client.search_audiobooks.return_value = [
            {
                "id": "42",
                "title": "BookLore Audio",
                "authors": "BookLore Author",
                "audiobookInfo": {
                    "tracks": [{"sizeBytes": 1048576}, {"sizeBytes": 1048576}],
                },
            }
        ]
        self.mock_container.mock_abs_client.get_all_audiobooks.return_value = []

        response = self.client.get("/api/forge/search_audio?q=booklore")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["audio_source"], "BookLore")
        self.assertEqual(payload[0]["audio_source_id"], "42")
        self.assertEqual(payload[0]["id"], "booklore:42")

    def test_forge_process_booklore_audio_uses_bridge_key_and_audio_kwargs(self):
        self.mock_container.mock_booklore_client.get_book_by_id.return_value = {
            "id": "42",
            "title": "BookLore Audio",
            "authors": "BookLore Author",
        }

        response = self.client.post(
            "/api/forge/process",
            json={
                "abs_id": "booklore:42",
                "audio_source": "BookLore",
                "audio_source_id": "42",
                "text_item": {"source": "Booklore", "booklore_id": "77"},
                "forge_stage_mode": "hardlink",
            },
        )

        self.assertEqual(response.status_code, 202)
        self.mock_container.mock_forge_service.start_manual_forge.assert_called_once_with(
            "booklore:42",
            {"source": "Booklore", "booklore_id": "77"},
            "BookLore Audio",
            "BookLore Author",
            audio_source="BookLore",
            audio_source_id="42",
            stage_mode="hardlink",
        )

    @patch("src.web_server.get_kosync_id_for_ebook", return_value="hash-forge-booklore")
    def test_match_forge_booklore_uses_bridge_key_identity(self, _mock_kosync):
        response = self.client.post(
            "/match",
            data={
                "action": "forge_match",
                "audio_source": "BookLore",
                "audio_source_id": "42",
                "audio_title": "BookLore Forge",
                "ebook_filename": "source.epub",
                "source_type": "Booklore",
                "source_id": "42",
                "source_path": "",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("/"))

        self.mock_container.mock_database_service.save_book.assert_called_once()
        staged_book = self.mock_container.mock_database_service.save_book.call_args[0][0]
        self.assertEqual(staged_book.abs_id, "booklore:42")
        self.assertEqual(staged_book.audio_source, "BookLore")
        self.assertEqual(staged_book.status, "forging")

        self.mock_container.mock_forge_service.start_auto_forge_match.assert_called_once()
        kwargs = self.mock_container.mock_forge_service.start_auto_forge_match.call_args.kwargs
        self.assertEqual(kwargs["abs_id"], "booklore:42")
        self.assertEqual(kwargs["audio_source"], "BookLore")
        self.assertEqual(kwargs["audio_source_id"], "42")

    @patch("src.web_server.get_kosync_id_for_ebook", return_value="hash-batch-1")
    def test_batch_match_add_and_process_queue(self, _mock_kosync):
        add_response = self.client.post(
            "/batch-match",
            data={
                "action": "add_to_queue",
                "audiobook_id": "ab-1",
                "ebook_filename": "batch.epub",
                "ebook_display_name": "Batch Book",
                "ebook_source_path": "/books/Author/Batch/batch.epub",
            },
        )
        self.assertEqual(add_response.status_code, 302)

        queue = web_server._load_match_queue()
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["abs_id"], "ab-1")

        process_response = self.client.post(
            "/batch-match",
            data={"action": "process_queue"},
        )
        self.assertEqual(process_response.status_code, 302)
        self.assertTrue(process_response.location.endswith("/"))

        self.mock_container.mock_database_service.save_book.assert_called_once()
        processed_book = self.mock_container.mock_database_service.save_book.call_args[0][0]
        self.assertEqual(processed_book.abs_id, "ab-1")
        self.assertEqual(processed_book.ebook_filename, "batch.epub")
        self.assertEqual(processed_book.kosync_doc_id, "hash-batch-1")
        self.assertEqual(
            _mock_kosync.call_args.kwargs.get("source_path"),
            "/books/Author/Batch/batch.epub",
        )

        self.assertEqual(web_server._load_match_queue(), [])

    def test_batch_match_audio_only_queue_creates_active_audio_mapping(self):
        self.mock_container.mock_database_service.get_book_by_audio_source.return_value = None
        self.mock_container.mock_database_service.save_book.side_effect = lambda book: book

        add_response = self.client.post(
            "/batch-match",
            data={
                "action": "add_to_queue",
                "audiobook_id": "ab-1",
                "audio_source": "ABS",
                "audio_source_id": "ab-1",
                "audio_title": "Regression Book",
                "audio_duration": "3600",
                "audio_only": "true",
            },
        )
        self.assertEqual(add_response.status_code, 302)
        queue = web_server._load_match_queue()
        self.assertEqual(len(queue), 1)
        self.assertTrue(queue[0]["audio_only"])
        self.assertEqual(queue[0]["ebook_filename"], "")

        process_response = self.client.post(
            "/batch-match",
            data={"action": "process_queue"},
        )
        self.assertEqual(process_response.status_code, 302)
        processed_book = self.mock_container.mock_database_service.save_book.call_args[0][0]
        self.assertEqual(processed_book.abs_id, "ab-1")
        self.assertEqual(processed_book.sync_mode, "audiobook_only")
        self.assertEqual(processed_book.status, "active")
        self.assertIsNone(processed_book.ebook_filename)
        self.assertEqual(web_server._load_match_queue(), [])

    def test_batch_match_audio_only_queue_succeeds_when_abs_lookup_misses(self):
        """The audio-only Add-to-Queue path must trust the title/duration already
        submitted from the rendered audiobook card rather than hard-requiring a
        fresh get_audiobooks_conditionally() lookup to resolve `audiobook_id`.

        That lookup returns a differently-shaped list (raw ABS dicts) than the one
        the card was actually rendered from (AudioResult records via
        get_searchable_audiobooks/_search_audiobooks_with_fallback), so a lookup
        miss is expected in real usage. Before the fix, any miss silently dropped
        the whole submission before it ever reached _match_queue_add -- no queue
        item, no error.
        """
        self.mock_container.mock_database_service.get_book_by_audio_source.return_value = None
        self.mock_container.mock_database_service.save_book.side_effect = lambda book: book

        # This id deliberately does NOT appear in MockContainer's
        # get_all_audiobooks() mock, simulating the real-world lookup miss.
        add_response = self.client.post(
            "/batch-match",
            data={
                "action": "add_to_queue",
                "audiobook_id": "ab-not-in-full-library-dump",
                "audio_source": "ABS",
                "audio_source_id": "ab-not-in-full-library-dump",
                "audio_title": "Untracked Audiobook",
                "audio_duration": "5400",
                "audio_only": "true",
            },
        )
        self.assertEqual(add_response.status_code, 302)

        queue = web_server._load_match_queue()
        self.assertEqual(len(queue), 1)
        self.assertTrue(queue[0]["audio_only"])
        self.assertEqual(queue[0]["abs_title"], "Untracked Audiobook")
        self.assertEqual(queue[0]["duration"], 5400.0)
        self.assertEqual(queue[0]["ebook_filename"], "")

    @patch("src.web_server._create_audio_only_mapping_from_queue_item")
    def test_suggestions_drains_shared_audio_only_queue(self, mock_audio_only):
        self.client.post(
            "/add-book",
            data={
                "action": "add_to_queue",
                "audiobook_id": "ab-1",
                "audio_source": "ABS",
                "audio_source_id": "ab-1",
                "audio_title": "Regression Book",
                "audio_duration": "3600",
                "audio_only": "true",
            },
        )

        with patch.object(
            web_server, "_match_queue_drain", wraps=web_server._match_queue_drain
        ) as mock_drain, patch.object(
            web_server,
            "_spawn_user_background",
            side_effect=lambda fn, *args, **_kwargs: fn(*args),
        ) as mock_spawn:
            response = self.client.post("/suggestions", data={"action": "process_queue"})

        self.assertEqual(response.status_code, 302)
        mock_drain.assert_called_once_with()
        self.assertIs(mock_spawn.call_args.args[0], web_server._process_batch_queue)
        self.assertEqual(mock_spawn.call_args.kwargs["label"], "batch-match-process")
        mock_audio_only.assert_called_once()
        self.assertTrue(mock_audio_only.call_args.args[0]["audio_only"])
        self.assertEqual(web_server._load_match_queue(), [])

    @patch("src.web_server._create_audio_only_mapping_from_queue_item")
    def test_forge_queue_actions_route_audio_only_items_without_forging(self, mock_audio_only):
        item = {
            "audio_only": True,
            "audio_source": "ABS",
            "audio_source_id": "ab-1",
            "abs_id": "ab-1",
            "abs_title": "Regression Book",
        }

        web_server._process_forge_match_queue([item])
        web_server._process_forge_only_queue([item])

        self.assertEqual(mock_audio_only.call_count, 2)
        self.mock_container.mock_forge_service.start_manual_forge.assert_not_called()

    @patch("src.web_server.get_kosync_id_for_ebook", return_value="hash-batch-forge-1")
    def test_batch_match_add_and_forge_queue_stages_without_storyteller(self, _mock_kosync):
        add_response = self.client.post(
            "/batch-match",
            data={
                "action": "add_to_queue",
                "audiobook_id": "ab-1",
                "ebook_filename": "batch-forge.epub",
                "ebook_display_name": "Batch Forge",
                "ebook_source": "Booklore",
                "ebook_source_id": "42",
                "ebook_source_path": "",
            },
        )
        self.assertEqual(add_response.status_code, 302)

        queue = web_server._load_match_queue()
        self.assertEqual(len(queue), 1)
        self.assertIsNone(queue[0]["ebook_source_path"])

        process_response = self.client.post(
            "/batch-match",
            data={"action": "forge_and_match_queue"},
        )
        self.assertEqual(process_response.status_code, 302)
        self.assertTrue(process_response.location.endswith("/"))

        self.mock_container.mock_database_service.save_book.assert_called_once()
        staged_book = self.mock_container.mock_database_service.save_book.call_args[0][0]
        self.assertEqual(staged_book.abs_id, "ab-1")
        self.assertEqual(staged_book.status, "forging")
        self.assertEqual(staged_book.ebook_filename, "batch-forge.epub")

        self.mock_container.mock_forge_service.start_auto_forge_match.assert_called_once()
        forge_kwargs = self.mock_container.mock_forge_service.start_auto_forge_match.call_args.kwargs
        self.assertEqual(forge_kwargs["abs_id"], "ab-1")
        self.assertEqual(forge_kwargs["text_item"]["source"], "Booklore")
        self.assertEqual(forge_kwargs["text_item"]["booklore_id"], "42")

        self.mock_container.mock_abs_client.add_to_collection.assert_not_called()

        self.assertEqual(web_server._load_match_queue(), [])

    @patch("src.web_server.ingest_storyteller_transcripts", return_value=None)
    @patch("src.web_server.get_kosync_id_for_ebook", return_value="hash-batch-forge-story")
    def test_batch_match_forge_queue_storyteller_items_use_direct_match(self, _mock_kosync, _mock_ingest):
        self.mock_container.mock_storyteller_client.download_book.return_value = True

        add_response = self.client.post(
            "/batch-match",
            data={
                "action": "add_to_queue",
                "audiobook_id": "ab-1",
                "ebook_filename": "batch-story.epub",
                "ebook_display_name": "Batch Story",
                "storyteller_uuid": "story-uuid-batch-forge",
            },
        )
        self.assertEqual(add_response.status_code, 302)

        process_response = self.client.post(
            "/batch-match",
            data={"action": "forge_and_match_queue"},
        )
        self.assertEqual(process_response.status_code, 302)

        self.mock_container.mock_database_service.save_book.assert_called_once()
        processed_book = self.mock_container.mock_database_service.save_book.call_args[0][0]
        self.assertEqual(processed_book.status, "pending")
        self.assertEqual(processed_book.storyteller_uuid, "story-uuid-batch-forge")
        self.mock_container.mock_forge_service.start_auto_forge_match.assert_not_called()

    @patch("src.web_server.get_kosync_id_for_ebook", return_value="hash-batch-forge-booklore")
    def test_batch_match_forge_queue_booklore_uses_bridge_key_identity(self, _mock_kosync):
        add_response = self.client.post(
            "/batch-match",
            data={
                "action": "add_to_queue",
                "audiobook_id": "",
                "audio_source": "BookLore",
                "audio_source_id": "42",
                "audio_title": "BookLore Batch Forge",
                "audio_cover_url": "/api/booklore/audiobook-cover/42",
                "audio_duration": "5123",
                "audio_provider_book_id": "42",
                "audio_provider_file_id": "991",
                "ebook_filename": "booklore-source.epub",
                "ebook_display_name": "BookLore Source",
                "ebook_source": "Booklore",
                "ebook_source_id": "6798",
            },
        )
        self.assertEqual(add_response.status_code, 302)

        process_response = self.client.post(
            "/batch-match",
            data={"action": "forge_and_match_queue"},
        )
        self.assertEqual(process_response.status_code, 302)

        self.mock_container.mock_database_service.save_book.assert_called_once()
        staged_book = self.mock_container.mock_database_service.save_book.call_args[0][0]
        self.assertEqual(staged_book.abs_id, "booklore:42")
        self.assertEqual(staged_book.audio_source, "BookLore")
        self.assertEqual(staged_book.status, "forging")

        self.mock_container.mock_forge_service.start_auto_forge_match.assert_called_once()
        kwargs = self.mock_container.mock_forge_service.start_auto_forge_match.call_args.kwargs
        self.assertEqual(kwargs["abs_id"], "booklore:42")
        self.assertEqual(kwargs["audio_source"], "BookLore")
        self.assertEqual(kwargs["audio_source_id"], "42")

    @patch("src.web_server.ingest_storyteller_transcripts", return_value=None)
    @patch("src.web_server.get_kosync_id_for_ebook", return_value="hash-batch-story-1")
    def test_batch_match_storyteller_uuid_preserves_storyteller_source(self, _mock_kosync, _mock_ingest):
        self.mock_container.mock_storyteller_client.download_book.return_value = True

        add_response = self.client.post(
            "/batch-match",
            data={
                "action": "add_to_queue",
                "audiobook_id": "ab-1",
                "ebook_filename": "batch-original.epub",
                "ebook_display_name": "Batch Story",
                "storyteller_uuid": "story-uuid-1",
            },
        )
        self.assertEqual(add_response.status_code, 302)

        process_response = self.client.post(
            "/batch-match",
            data={"action": "process_queue"},
        )
        self.assertEqual(process_response.status_code, 302)

        self.mock_container.mock_database_service.save_book.assert_called_once()
        processed_book = self.mock_container.mock_database_service.save_book.call_args[0][0]
        self.assertEqual(processed_book.storyteller_uuid, "story-uuid-1")
        self.assertEqual(processed_book.transcript_source, "storyteller")
        self.assertIsNone(processed_book.transcript_file)

        self.assertEqual(web_server._load_match_queue(), [])

    @patch("src.web_server.get_kosync_id_for_ebook", return_value="hash-batch-story-real")
    def test_batch_match_storyteller_uuid_real_ingest_persists_manifest(self, _mock_kosync):
        self._prepare_storyteller_assets("Regression Book", chapter_count=2)
        self._set_abs_chapters(chapter_count=2)
        self.mock_container.mock_storyteller_client.download_book.return_value = True

        add_response = self.client.post(
            "/batch-match",
            data={
                "action": "add_to_queue",
                "audiobook_id": "ab-1",
                "ebook_filename": "batch-original.epub",
                "ebook_display_name": "Batch Story Real",
                "storyteller_uuid": "story-uuid-batch-real",
            },
        )
        self.assertEqual(add_response.status_code, 302)

        process_response = self.client.post("/batch-match", data={"action": "process_queue"})
        self.assertEqual(process_response.status_code, 302)

        self.mock_container.mock_database_service.save_book.assert_called_once()
        saved_book = self.mock_container.mock_database_service.save_book.call_args[0][0]
        self.assertEqual(saved_book.storyteller_uuid, "story-uuid-batch-real")
        self.assertEqual(saved_book.transcript_source, "storyteller")
        self.assertIsNotNone(saved_book.transcript_file)
        self.assertTrue(Path(saved_book.transcript_file).exists())

    @patch("src.web_server.ingest_storyteller_transcripts", return_value=None)
    @patch("src.web_server.get_kosync_id_for_ebook", side_effect=[None, "hash-batch-story-fallback"])
    def test_batch_match_storyteller_uuid_falls_back_to_artifact_hash_when_original_missing(self, _mock_kosync, _mock_ingest):
        self.mock_container.mock_storyteller_client.download_book.return_value = True
        self.mock_container.mock_booklore_client.find_book_by_filename.return_value = None

        add_response = self.client.post(
            "/batch-match",
            data={
                "action": "add_to_queue",
                "audiobook_id": "ab-1",
                "ebook_filename": "batch-original.epub",
                "ebook_display_name": "Batch Story Fallback",
                "storyteller_uuid": "story-uuid-batch-fallback",
            },
        )
        self.assertEqual(add_response.status_code, 302)

        process_response = self.client.post("/batch-match", data={"action": "process_queue"})
        self.assertEqual(process_response.status_code, 302)

        saved_book = self.mock_container.mock_database_service.save_book.call_args[0][0]
        self.assertEqual(saved_book.kosync_doc_id, "hash-batch-story-fallback")
        call_args = [call.args for call in _mock_kosync.call_args_list]
        self.assertEqual(call_args[0], ("batch-original.epub", None))
        self.assertEqual(call_args[1], ("storyteller_story-uuid-batch-fallback.epub",))

    def test_batch_match_remove_from_queue(self):
        web_server._save_match_queue([
            {"abs_id": "ab-1"},
            {"abs_id": "ab-2"},
        ])

        response = self.client.post(
            "/batch-match",
            data={"action": "remove_from_queue", "abs_id": "ab-1"},
        )
        self.assertEqual(response.status_code, 302)

        queue = web_server._load_match_queue()
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["abs_id"], "ab-2")

    def test_suggestions_queue_add_clear_xhr_returns_panel_fragment(self):
        # An XHR add/clear returns the re-rendered queue panel fragment (200) instead of a
        # redirect, so the page swaps it in place without reloading (preserving scroll).
        self.mock_container.mock_abs_client.get_all_audiobooks.reset_mock()
        add_response = self.client.post(
            "/suggestions",
            data={
                "action": "add_to_queue",
                "audiobook_id": "ab-1",
                "audio_source": "ABS",
                "audio_source_id": "ab-1",
                "ebook_filename": "suggested.epub",
                "ebook_display_name": "Suggested Book",
            },
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        self.assertEqual(add_response.status_code, 200)
        body = add_response.get_data(as_text=True)
        self.assertIn("Regression Book", body)
        self.assertIn("Match All", body)
        queue = web_server._load_match_queue()
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["abs_title"], "Regression Book")
        self.assertEqual(queue[0]["duration"], 3600)
        self.mock_container.mock_abs_client.get_all_audiobooks.assert_called_once_with()

        clear_response = self.client.post(
            "/suggestions",
            data={"action": "clear_queue"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        self.assertEqual(clear_response.status_code, 200)
        self.assertIn("Queue is empty", clear_response.get_data(as_text=True))
        self.assertEqual(web_server._load_match_queue(), [])

    def test_match_form_aliases_and_ebook_only_work_across_routes(self):
        cases = (
            (51, "/add-book", {
                "audiobook_id": "ab-alias",
                "audio_source": "ABS",
                "audio_source_id": "ab-alias",
                "audio_title": "Alias Audio",
                "audio_duration": "90",
                "ebook_filename": "alias.epub",
                "source_type": "BookOrbit",
                "source_id": "bo-alias",
                "source_path": "/books/alias.epub",
            }, "ab-alias"),
            (52, "/suggestions", {
                "ebook_filename": "ebook-only.epub",
                "ebook_display_name": "Ebook Only",
                "source_type": "BookLore",
                "source_id": "bl-ebook-only",
                "source_path": "/books/ebook-only.epub",
            }, "ebook:ebook-only.epub"),
        )
        for user_id, endpoint, form_data, expected_id in cases:
            with self.subTest(endpoint=endpoint):
                self._post_as_user(
                    user_id, endpoint, {"action": "add_to_queue", **form_data}
                )
                item = self._load_queue_as_user(user_id)[0]
                self.assertEqual(item["abs_id"], expected_id)
                self.assertEqual(item["ebook_source"], form_data["source_type"])
                self.assertEqual(item["ebook_source_id"], form_data["source_id"])
                self.assertEqual(item["ebook_source_path"], form_data["source_path"])
        self.assertIsNone(self._load_queue_as_user(52)[0]["audio_source"])

    def test_suggestions_add_many_to_queue_bulk(self):
        # Bulk add posts suggestion keys; the server builds each queue item from its
        # cached suggestion's top match (used by "Add all exact" / "Add selected").
        with self.client.session_transaction() as session_data:
            session_data["suggestions_state_id"] = "state-bulk"
        with web_server.SUGGESTIONS_STATE_LOCK:
            web_server.SUGGESTIONS_STATE_STORE["state-bulk"] = {
                "scan_results": [],
                "scan_cache_by_abs": {
                    "ab-1": {
                        "bridge_key": "ab-1", "abs_id": "ab-1",
                        "audio_source": "ABS", "audio_source_id": "ab-1",
                        "audio_title": "Exact Audio", "audio_duration": 3600,
                        "audio_cover_url": "",
                        "matches": [{
                            "ebook_filename": "exact.epub", "display_name": "Exact Ebook",
                            "source": "Grimmory", "source_id": "g-1",
                            "source_path": "/books/x/exact.epub",
                            "score": 100.0, "match_reason": "same_folder",
                        }],
                    },
                    "ab-2": {
                        "bridge_key": "ab-2", "abs_id": "ab-2",
                        "audio_source": "ABS", "audio_source_id": "ab-2",
                        "audio_title": "Fuzzy Audio", "audio_duration": 3600,
                        "audio_cover_url": "",
                        "matches": [{
                            "ebook_filename": "fuzzy.epub", "display_name": "Fuzzy Ebook",
                            "source": "Grimmory", "source_id": "g-2",
                            "source_path": "", "score": 88.0,
                        }],
                    },
                },
                "scan_cache_no_match_abs_ids": [],
                "scan_last_stats": {},
                "scan_has_run": True,
                "updated_at": time.time(),
            }

        response = self.client.post(
            "/suggestions",
            data={"action": "add_many_to_queue", "bridge_keys": ["ab-1", "ab-2"]},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        self.assertEqual(response.status_code, 200)

        queue = web_server._load_match_queue()
        self.assertEqual({item["bridge_key"] for item in queue}, {"ab-1", "ab-2"})
        exact_item = next(i for i in queue if i["bridge_key"] == "ab-1")
        self.assertEqual(exact_item["ebook_filename"], "exact.epub")
        self.assertEqual(exact_item["ebook_source"], "Grimmory")
        self.assertEqual(exact_item["ebook_source_path"], "/books/x/exact.epub")
        self.assertEqual(exact_item["storyteller_uuid"], "")

    @patch("src.web_server.get_kosync_id_for_ebook", return_value="hash-sugg-forge-1")
    def test_suggestions_forge_and_match_queue(self, _mock_kosync):
        # The Suggestions page can run the same forge/match-all path as Add Book, so the
        # user no longer has to switch pages to process the queue.
        self.client.post(
            "/suggestions",
            data={
                "action": "add_to_queue",
                "audiobook_id": "ab-1",
                "audio_source": "ABS",
                "audio_source_id": "ab-1",
                "ebook_filename": "sugg-forge.epub",
                "ebook_display_name": "Sugg Forge",
                "ebook_source": "Booklore",
                "ebook_source_id": "55",
            },
        )
        self.assertEqual(len(web_server._load_match_queue()), 1)

        response = self.client.post("/suggestions", data={"action": "forge_and_match_queue"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("/"))
        self.assertEqual(web_server._load_match_queue(), [])
        self.mock_container.mock_forge_service.start_auto_forge_match.assert_called_once()

    @patch("src.web_server._start_suggestions_scan_job", return_value="job-1")
    def test_suggestions_scan_ajax_and_status(self, _mock_start_job):
        scan_response = self.client.post(
            "/suggestions",
            data={"action": "scan"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        self.assertEqual(scan_response.status_code, 200)
        payload = scan_response.get_json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["status"], "running")
        self.assertEqual(payload["job_id"], "job-1")

        with self.client.session_transaction() as session_data:
            self.assertEqual(session_data.get("suggestions_scan_job_id"), "job-1")

        with patch(
            "src.web_server._get_suggestions_scan_job",
            return_value={
                "status": "running",
                "error": None,
                "progress": {"phase": "scanning", "percent": 40},
            },
        ):
            status_response = self.client.get("/api/suggestions/scan-status")
            self.assertEqual(status_response.status_code, 200)
            status_payload = status_response.get_json()
            self.assertEqual(status_payload["status"], "running")
            self.assertEqual(status_payload["progress"]["percent"], 40)

        with patch(
            "src.web_server._get_suggestions_scan_job",
            return_value={
                "status": "done",
                "error": None,
                "progress": {"phase": "finalizing", "percent": 100},
                "results": {
                    "suggestions": [{"abs_id": "ab-1"}, {"abs_id": "ab-2"}],
                    "stats": {"scanned_new": 2, "reused_cached": 0},
                },
            },
        ):
            done_response = self.client.get("/api/suggestions/scan-status")
            self.assertEqual(done_response.status_code, 200)
            done_payload = done_response.get_json()
            self.assertEqual(done_payload["status"], "done")
            self.assertEqual(done_payload["count"], 2)
            self.assertEqual(done_payload["stats"]["scanned_new"], 2)

    @patch("src.web_server.render_template", return_value="ok")
    def test_suggestions_page_dedupes_same_source_title_author(self, _mock_render):
        import src.web_server as web_server

        self.mock_container.mock_database_service.get_all_books.return_value = []

        with self.client.session_transaction() as session_data:
            session_data["suggestions_state_id"] = "state-dedupe"

        with web_server.SUGGESTIONS_STATE_LOCK:
            web_server.SUGGESTIONS_STATE_STORE["state-dedupe"] = {
                "scan_results": [
                    {
                        "bridge_key": "ab-duplicate-1",
                        "abs_id": "ab-duplicate-1",
                        "audio_source": "ABS",
                        "audio_title": "Dark Hollow",
                        "audio_author": "Brian Keene",
                        "matches": [{"display_name": "dark-hollow.epub", "score": 92.0}],
                    },
                    {
                        "bridge_key": "ab-duplicate-2",
                        "abs_id": "ab-duplicate-2",
                        "audio_source": "ABS",
                        "audio_title": "Dark Hollow",
                        "audio_author": "Brian Keene",
                        "matches": [{"display_name": "dark-hollow-alt.epub", "score": 89.0}],
                    },
                    {
                        "bridge_key": "ab-unique-1",
                        "abs_id": "ab-unique-1",
                        "audio_source": "ABS",
                        "audio_title": "Unique Title",
                        "audio_author": "Unique Author",
                        "matches": [{"display_name": "unique.epub", "score": 85.0}],
                    },
                ],
                "scan_cache_by_abs": {
                    "ab-duplicate-1": {"bridge_key": "ab-duplicate-1"},
                    "ab-duplicate-2": {"bridge_key": "ab-duplicate-2"},
                    "ab-unique-1": {"bridge_key": "ab-unique-1"},
                },
                "scan_cache_no_match_abs_ids": [],
                "scan_last_stats": {},
                "scan_has_run": True,
                "created_at": time.time(),
                "updated_at": time.time(),
            }

        response = self.client.get("/suggestions")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"ok")

        rendered = _mock_render.call_args.kwargs["suggestions"]
        rendered_keys = [(s.get("bridge_key") or s.get("abs_id")) for s in rendered]
        self.assertEqual(rendered_keys, ["ab-duplicate-1", "ab-unique-1"])

        with web_server.SUGGESTIONS_STATE_LOCK:
            updated_state = web_server.SUGGESTIONS_STATE_STORE["state-dedupe"]
            self.assertEqual(len(updated_state.get("scan_results", [])), 2)
            self.assertNotIn("ab-duplicate-2", updated_state.get("scan_cache_by_abs", {}))

    @patch("src.web_server.render_template", return_value="ok")
    def test_suggestions_page_filters_active_booklore_legacy_mapping(self, _mock_render):
        import src.web_server as web_server

        active_book = Mock()
        active_book.abs_id = "booklore_audio_8655"
        active_book.audio_source = "BookLore"
        active_book.audio_source_id = "8655"
        self.mock_container.mock_database_service.get_all_books.return_value = [active_book]

        with self.client.session_transaction() as session_data:
            session_data["suggestions_state_id"] = "state-legacy"

        with web_server.SUGGESTIONS_STATE_LOCK:
            web_server.SUGGESTIONS_STATE_STORE["state-legacy"] = {
                "scan_results": [
                    {
                        "bridge_key": "booklore:8655",
                        "abs_id": "booklore:8655",
                        "audio_source": "BookLore",
                        "audio_source_id": "8655",
                        "audio_title": "Legacy BookLore",
                        "audio_author": "Test Author",
                        "audio_cover_url": "/api/booklore/audiobook-cover/8655",
                        "matches": [{"display_name": "legacy.epub", "score": 88.0}],
                    }
                ],
                "scan_cache_by_abs": {
                    "booklore:8655": {
                        "bridge_key": "booklore:8655",
                        "abs_id": "booklore:8655",
                        "audio_source": "BookLore",
                        "audio_source_id": "8655",
                        "audio_title": "Legacy BookLore",
                        "audio_author": "Test Author",
                        "audio_cover_url": "/api/booklore/audiobook-cover/8655",
                        "matches": [{"display_name": "legacy.epub", "score": 88.0}],
                    }
                },
                "scan_cache_no_match_abs_ids": ["booklore:8655"],
                "scan_last_stats": {},
                "scan_has_run": True,
                "created_at": time.time(),
                "updated_at": time.time(),
            }

        response = self.client.get("/suggestions")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"ok")

        with web_server.SUGGESTIONS_STATE_LOCK:
            updated_state = web_server.SUGGESTIONS_STATE_STORE["state-legacy"]
            self.assertEqual(updated_state.get("scan_results", []), [])
            self.assertEqual(updated_state.get("scan_cache_by_abs", {}), {})
            self.assertEqual(updated_state.get("scan_cache_no_match_abs_ids", []), [])

    @patch("src.web_server.get_kosync_id_for_ebook", return_value="hash-suggestions-1")
    def test_suggestions_queue_processes_bookorbit_ebook_and_claims_user(self, _mock_kosync):
        self.mock_container.mock_database_service.get_kosync_doc_by_filename.return_value = Mock(
            document_hash="device-hash"
        )
        add_response = self._post_as_user(
            41,
            "/suggestions",
            {
                "action": "add_to_queue",
                "audiobook_id": "ab-1",
                "ebook_filename": "suggested.epub",
                "ebook_display_name": "Suggested Book",
                "ebook_source": "BookOrbit",
                "ebook_source_id": "bo-17",
                "ebook_source_path": "/books/Author/Suggested/suggested.epub",
            },
        )
        self.assertEqual(add_response.status_code, 302)

        queue = self._load_queue_as_user(41)
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["abs_id"], "ab-1")

        process_response = self._post_as_user(
            41, "/suggestions", {"action": "process_queue"}
        )
        self.assertEqual(process_response.status_code, 302)

        self.mock_container.mock_database_service.save_book.assert_called_once()
        self.assertEqual(
            _mock_kosync.call_args.kwargs.get("source_path"),
            "/books/Author/Suggested/suggested.epub",
        )
        self.assertEqual(_mock_kosync.call_args.kwargs.get("bookorbit_id"), "bo-17")
        self.mock_container.mock_database_service.link_user_book.assert_called_once_with(41, "ab-1")
        dismissed = {
            call.args[0]
            for call in self.mock_container.mock_database_service.dismiss_suggestion.call_args_list
        }
        self.assertEqual(dismissed, {"ab-1", "hash-suggestions-1", "device-hash"})
        self.assertEqual(self._load_queue_as_user(41), [])

    def test_library_audio_captures_bookorbit_shelf_origin_before_mapping(self):
        add_response = self._post_as_user(
            42,
            "/suggestions",
            {
                "action": "add_to_queue",
                "audiobook_id": "booklore:42",
                "audio_source_id": "42",
                "audio_title": "BookLore Regression",
                "audio_cover_url": "/api/booklore/audiobook-cover/42",
                "audio_duration": "5123",
                "audio_provider_book_id": "42",
                "audio_provider_file_id": "991",
                "ebook_filename": "booklore-suggested.epub",
                "ebook_display_name": "BookLore Suggested",
                "ebook_source": "BookLore",
                "ebook_source_id": "6798",
                "ebook_source_path": "/books/BookLore/booklore-suggested.epub",
            },
        )
        self.assertEqual(add_response.status_code, 302)

        queue = self._load_queue_as_user(42)
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["abs_id"], "booklore:42")
        self.assertEqual(queue[0]["audio_source"], "BookLore")

        pending = Mock(
            origin="shelf_watch",
            origin_metadata={
                "source_name": "BookOrbit",
                "grimmory_filename": "bookorbit-origin.epub",
            },
        )
        events = []

        def lookup_pending(key):
            events.append(f"lookup:{key}")
            return pending if key == "booklore:42" else None

        def save_mapping(**_kwargs):
            events.append("mapping")
            pending.origin = "dismissed"
            return Mock(abs_id="booklore:42"), None, None

        self.mock_container.mock_database_service.get_pending_suggestion.side_effect = lookup_pending
        with patch.dict(
            os.environ, {"BOOKORBIT_SHELF_WATCH_NAME": "Reading Next"}, clear=False
        ), patch(
            "src.web_server._create_or_update_library_audio_mapping",
            side_effect=save_mapping,
        ) as mock_mapping:
            self._post_as_user(42, "/suggestions", {"action": "process_queue"})

        self.assertEqual(events[:2], ["lookup:booklore:42", "mapping"])
        mock_mapping.assert_called_once()
        call_kwargs = mock_mapping.call_args.kwargs
        self.assertEqual(call_kwargs["audio_source_id"], "42")
        self.assertEqual(call_kwargs["audio_title"], "BookLore Regression")
        self.assertEqual(call_kwargs["ebook_filename"], "booklore-suggested.epub")
        self.assertEqual(call_kwargs["ebook_source"], "BookLore")
        self.assertEqual(call_kwargs["ebook_source_id"], "6798")
        self.assertEqual(call_kwargs["ebook_source_path"], "/books/BookLore/booklore-suggested.epub")

        self.mock_container.mock_bookorbit_client.remove_from_shelf.assert_called_once_with(
            "bookorbit-origin.epub", "Reading Next"
        )
        self.mock_container.mock_bookorbit_client.move_between_shelves.assert_not_called()
        self.mock_container.mock_database_service.link_user_book.assert_called_once_with(
            42, "booklore:42"
        )
        self.mock_container.mock_database_service.save_book.assert_not_called()
        self.assertEqual(self._load_queue_as_user(42), [])

    @patch("src.web_server.get_kosync_id_for_ebook", return_value="hash-forge-shelf-watch")
    def test_forge_queue_completes_bookorbit_shelf_watch_before_dismissal(self, _mock_kosync):
        pending = Mock(
            origin="shelf_watch",
            origin_metadata={
                "source_name": "BookOrbit",
                "grimmory_filename": "bookorbit-origin.epub",
            },
        )
        events = []

        def lookup_pending(key):
            events.append(f"lookup:{key}")
            return pending if key == "ab-1" and pending.origin == "shelf_watch" else None

        def dismiss_suggestion(key):
            events.append(f"dismiss:{key}")
            pending.origin = "dismissed"

        self.mock_container.mock_database_service.get_pending_suggestion.side_effect = lookup_pending
        self.mock_container.mock_database_service.dismiss_suggestion.side_effect = dismiss_suggestion
        self.mock_container.mock_forge_service.start_auto_forge_match.side_effect = (
            lambda **_kwargs: events.append("forge")
        )
        self.mock_container.mock_bookorbit_client.move_between_shelves.side_effect = (
            lambda *_args: events.append("move") or True
        )

        item = {
            "abs_id": "ab-1",
            "abs_title": "Regression Book",
            "audio_source": "ABS",
            "audio_source_id": "ab-1",
            "ebook_filename": "source.epub",
            "ebook_source": "BookOrbit",
            "ebook_source_id": "bo-17",
            "duration": 3600,
        }

        with patch.dict(
            os.environ, {"BOOKORBIT_SHELF_WATCH_NAME": "Reading Next"}, clear=False
        ):
            web_server._process_forge_match_queue([item])

        self.assertEqual(events[:3], ["lookup:ab-1", "forge", "move"])
        self.assertLess(events.index("move"), events.index("dismiss:ab-1"))
        self.mock_container.mock_bookorbit_client.move_between_shelves.assert_called_once_with(
            "bookorbit-origin.epub", "Reading Next", "Kobo"
        )

    def test_forge_queue_library_audio_removes_shelf_watch_source_after_mapping(self):
        pending = Mock(
            origin="shelf_watch",
            origin_metadata={
                "source_name": "BookOrbit",
                "grimmory_filename": "bookorbit-origin.epub",
            },
        )
        self.mock_container.mock_database_service.get_pending_suggestion.side_effect = (
            lambda key: pending if key == "booklore:42" else None
        )

        with patch.dict(
            os.environ, {"BOOKORBIT_SHELF_WATCH_NAME": "Reading Next"}, clear=False
        ), patch(
            "src.web_server._create_or_update_library_audio_mapping",
            return_value=(Mock(abs_id="booklore:42"), None, None),
        ):
            web_server._process_forge_match_queue([
                {
                    "abs_id": "booklore:42",
                    "audio_source": "BookLore",
                    "audio_source_id": "42",
                    "audio_title": "BookLore Regression",
                    "ebook_filename": "source.epub",
                    "ebook_source": "BookLore",
                    "ebook_source_id": "6798",
                    "storyteller_uuid": "story-42",
                }
            ])

        self.mock_container.mock_bookorbit_client.remove_from_shelf.assert_called_once_with(
            "bookorbit-origin.epub", "Reading Next"
        )
        self.mock_container.mock_bookorbit_client.move_between_shelves.assert_not_called()

    def test_early_queue_mappings_complete_shelf_watch_approval_after_success(self):
        metadata = {
            "source_name": "BookOrbit",
            "grimmory_filename": "bookorbit-origin.epub",
        }
        cases = (
            (
                web_server._process_batch_queue,
                {"audio_source": "ABS", "audio_only": True, "abs_id": "ab-1"},
                "_create_audio_only_mapping_from_queue_item",
            ),
            (
                web_server._process_forge_match_queue,
                {"audio_source": "ABS", "audio_only": True, "abs_id": "ab-1"},
                "_create_audio_only_mapping_from_queue_item",
            ),
            (
                web_server._process_batch_queue,
                {"ebook_filename": "ebook-only.epub"},
                "_create_ebook_only_mapping_from_queue_item",
            ),
            (
                web_server._process_forge_match_queue,
                {"ebook_filename": "ebook-only.epub"},
                "_create_ebook_only_mapping_from_queue_item",
            ),
        )

        for processor, item, mapping_name in cases:
            with patch(
                "src.web_server._queue_item_shelf_watch_metadata", return_value=metadata
            ), patch(
                f"src.web_server.{mapping_name}", return_value=Mock(abs_id="saved")
            ) as mock_mapping, patch(
                "src.web_server._complete_shelf_watch_approval"
            ) as mock_complete:
                processor([item])

            mock_mapping.assert_called_once_with(item)
            mock_complete.assert_called_once_with(metadata)

    @patch("src.web_server.ingest_storyteller_transcripts", return_value=None)
    @patch("src.web_server.get_kosync_id_for_ebook", return_value="hash-shelf-fallback")
    def test_batch_queue_falls_back_when_shelf_watch_move_fails(self, _mock_kosync, _mock_ingest):
        pending = Mock(
            origin="shelf_watch",
            origin_metadata={
                "source_name": "BookOrbit",
                "grimmory_filename": "bookorbit-origin.epub",
            },
        )
        self.mock_container.mock_database_service.get_pending_suggestion.side_effect = (
            lambda key: pending if key == "ab-1" else None
        )
        self.mock_container.mock_bookorbit_client.move_between_shelves.return_value = False
        item = {
            "abs_id": "ab-1",
            "abs_title": "Regression Book",
            "audio_source": "ABS",
            "audio_source_id": "ab-1",
            "ebook_filename": "source.epub",
            "ebook_source": "BookOrbit",
            "ebook_source_id": "bo-17",
            "duration": 3600,
        }

        with patch("src.web_server._shelve_matched_ebook") as mock_shelve:
            web_server._process_batch_queue([item])

        self.mock_container.mock_bookorbit_client.move_between_shelves.assert_called_once_with(
            "bookorbit-origin.epub", "Up Next", "Kobo"
        )
        mock_shelve.assert_called_once_with("source.epub", "BookOrbit", "bo-17")

    @patch(
        "src.web_server._create_or_update_bookfusion_progress_mapping",
        return_value=(Mock(abs_id="ab-1"), None, None),
    )
    def test_suggestions_bookfusion_preserves_audio_fields_and_claims_user(self, mock_mapping):
        self._post_as_user(
            43,
            "/suggestions",
            {
                "action": "add_to_queue",
                "audiobook_id": "ab-1",
                "audio_source": "ABS",
                "audio_source_id": "ab-1",
                "audio_title": "BookFusion Audio",
                "audio_cover_url": "/covers/ab-1",
                "audio_duration": "4321.5",
                "audio_provider_book_id": "provider-book-1",
                "audio_provider_file_id": "provider-file-1",
                "ebook_filename": "bookfusion.epub",
                "ebook_display_name": "BookFusion Edition",
                "ebook_source": "BookFusion",
                "ebook_source_id": "bf-77",
                "storyteller_uuid": "story-bf-1",
            },
        )

        self._post_as_user(43, "/suggestions", {"action": "process_queue"})

        self.assertEqual(
            mock_mapping.call_args.kwargs,
            {
                "audio_source": "ABS",
                "audio_source_id": "ab-1",
                "audio_title": "BookFusion Audio",
                "audio_cover_url": "/covers/ab-1",
                "audio_duration": 4321.5,
                "audio_provider_book_id": "provider-book-1",
                "audio_provider_file_id": "provider-file-1",
                "bookfusion_id": "bf-77",
                "bookfusion_title": "BookFusion Edition",
                "storyteller_uuid": "story-bf-1",
            },
        )
        self.mock_container.mock_database_service.link_user_book.assert_called_once_with(43, "ab-1")
        self.assertEqual(self._load_queue_as_user(43), [])

    @patch("src.web_server.ingest_storyteller_transcripts", return_value=None)
    @patch("src.web_server.get_kosync_id_for_ebook", return_value="hash-suggestions-story-1")
    def test_suggestions_queue_storyteller_uuid_preserves_storyteller_source(self, _mock_kosync, _mock_ingest):
        self.mock_container.mock_storyteller_client.download_book.return_value = True

        add_response = self.client.post(
            "/suggestions",
            data={
                "action": "add_to_queue",
                "audiobook_id": "ab-1",
                "ebook_filename": "suggested-original.epub",
                "ebook_display_name": "Suggested Story",
                "storyteller_uuid": "story-uuid-2",
            },
        )
        self.assertEqual(add_response.status_code, 302)

        process_response = self.client.post(
            "/suggestions",
            data={"action": "process_queue"},
        )
        self.assertEqual(process_response.status_code, 302)
        self.assertTrue(process_response.location.endswith("/"))

        self.mock_container.mock_database_service.save_book.assert_called_once()
        processed_book = self.mock_container.mock_database_service.save_book.call_args[0][0]
        self.assertEqual(processed_book.storyteller_uuid, "story-uuid-2")
        self.assertEqual(processed_book.transcript_source, "storyteller")
        self.assertIsNone(processed_book.transcript_file)

        self.assertEqual(web_server._load_match_queue(), [])

    @patch("src.web_server.get_kosync_id_for_ebook", return_value="hash-suggestions-story-real")
    def test_suggestions_queue_storyteller_uuid_real_ingest_persists_manifest(self, _mock_kosync):
        self._prepare_storyteller_assets("Regression Book", chapter_count=2)
        self._set_abs_chapters(chapter_count=2)
        self.mock_container.mock_storyteller_client.download_book.return_value = True

        add_response = self.client.post(
            "/suggestions",
            data={
                "action": "add_to_queue",
                "audiobook_id": "ab-1",
                "ebook_filename": "suggested-original.epub",
                "ebook_display_name": "Suggested Story Real",
                "storyteller_uuid": "story-uuid-suggestions-real",
            },
        )
        self.assertEqual(add_response.status_code, 302)

        process_response = self.client.post("/suggestions", data={"action": "process_queue"})
        self.assertEqual(process_response.status_code, 302)

        self.mock_container.mock_database_service.save_book.assert_called_once()
        saved_book = self.mock_container.mock_database_service.save_book.call_args[0][0]
        self.assertEqual(saved_book.storyteller_uuid, "story-uuid-suggestions-real")
        self.assertEqual(saved_book.transcript_source, "storyteller")
        self.assertIsNotNone(saved_book.transcript_file)
        self.assertTrue(Path(saved_book.transcript_file).exists())

    @patch("src.web_server.ingest_storyteller_transcripts", return_value=None)
    @patch("src.web_server.get_kosync_id_for_ebook", side_effect=[None, "hash-suggestions-story-fallback"])
    def test_suggestions_queue_storyteller_uuid_falls_back_to_artifact_hash_when_original_missing(self, _mock_kosync, _mock_ingest):
        self.mock_container.mock_storyteller_client.download_book.return_value = True
        self.mock_container.mock_booklore_client.find_book_by_filename.return_value = None

        add_response = self.client.post(
            "/suggestions",
            data={
                "action": "add_to_queue",
                "audiobook_id": "ab-1",
                "ebook_filename": "suggested-original.epub",
                "ebook_display_name": "Suggested Story Fallback",
                "storyteller_uuid": "story-uuid-suggestions-fallback",
            },
        )
        self.assertEqual(add_response.status_code, 302)

        process_response = self.client.post("/suggestions", data={"action": "process_queue"})
        self.assertEqual(process_response.status_code, 302)

        saved_book = self.mock_container.mock_database_service.save_book.call_args[0][0]
        self.assertEqual(saved_book.kosync_doc_id, "hash-suggestions-story-fallback")
        call_args = [call.args for call in _mock_kosync.call_args_list]
        self.assertEqual(call_args[0], ("suggested-original.epub", None))
        self.assertEqual(call_args[1], ("storyteller_story-uuid-suggestions-fallback.epub",))

    # -- STORYTELLER_NO_EPUB_CACHE flag honored in batch flows --

    def _enable_no_cache_with_resolvable_original(self, original_name: str):
        """Drop a real EPUB file on disk and wire resolve_book_path to it."""
        original_path = Path(self.temp_dir) / original_name
        original_path.write_bytes(b"epub bytes")
        self.mock_container.mock_ebook_parser.resolve_book_path.return_value = original_path
        os.environ["STORYTELLER_NO_EPUB_CACHE"] = "true"
        self.addCleanup(lambda: os.environ.pop("STORYTELLER_NO_EPUB_CACHE", None))
        return original_path

    @patch("src.web_server.ingest_storyteller_transcripts", return_value=None)
    @patch("src.web_server.get_kosync_id_for_ebook", return_value="hash-batch-forge-nocache")
    def test_batch_forge_queue_no_epub_cache_uses_original_epub(self, _mock_kosync, _mock_ingest):
        self._enable_no_cache_with_resolvable_original("batch-forge-original.epub")

        add_response = self.client.post(
            "/batch-match",
            data={
                "action": "add_to_queue",
                "audiobook_id": "ab-1",
                "ebook_filename": "batch-forge-original.epub",
                "ebook_display_name": "Batch Forge No Cache",
                "storyteller_uuid": "story-uuid-forge-nocache",
            },
        )
        self.assertEqual(add_response.status_code, 302)

        process_response = self.client.post(
            "/batch-match",
            data={"action": "forge_and_match_queue"},
        )
        self.assertEqual(process_response.status_code, 302)

        self.mock_container.mock_storyteller_client.download_book.assert_not_called()
        self.mock_container.mock_database_service.save_book.assert_called_once()
        saved_book = self.mock_container.mock_database_service.save_book.call_args[0][0]
        self.assertEqual(saved_book.ebook_filename, "batch-forge-original.epub")
        self.assertEqual(saved_book.original_ebook_filename, "batch-forge-original.epub")
        self.assertEqual(saved_book.storyteller_uuid, "story-uuid-forge-nocache")

    @patch("src.web_server.ingest_storyteller_transcripts", return_value=None)
    @patch("src.web_server.get_kosync_id_for_ebook", return_value="hash-batch-match-nocache")
    def test_batch_match_process_queue_no_epub_cache_uses_original_epub(self, _mock_kosync, _mock_ingest):
        self._enable_no_cache_with_resolvable_original("batch-match-original.epub")

        add_response = self.client.post(
            "/batch-match",
            data={
                "action": "add_to_queue",
                "audiobook_id": "ab-1",
                "ebook_filename": "batch-match-original.epub",
                "ebook_display_name": "Batch Match No Cache",
                "storyteller_uuid": "story-uuid-match-nocache",
            },
        )
        self.assertEqual(add_response.status_code, 302)

        process_response = self.client.post(
            "/batch-match",
            data={"action": "process_queue"},
        )
        self.assertEqual(process_response.status_code, 302)

        self.mock_container.mock_storyteller_client.download_book.assert_not_called()
        self.mock_container.mock_database_service.save_book.assert_called_once()
        saved_book = self.mock_container.mock_database_service.save_book.call_args[0][0]
        self.assertEqual(saved_book.ebook_filename, "batch-match-original.epub")
        self.assertEqual(saved_book.original_ebook_filename, "batch-match-original.epub")
        self.assertEqual(saved_book.storyteller_uuid, "story-uuid-match-nocache")

    @patch("src.web_server.ingest_storyteller_transcripts", return_value=None)
    @patch("src.web_server.get_kosync_id_for_ebook", return_value="hash-suggestions-nocache")
    def test_suggestions_process_queue_no_epub_cache_uses_original_epub(self, _mock_kosync, _mock_ingest):
        self._enable_no_cache_with_resolvable_original("suggestions-original.epub")

        add_response = self.client.post(
            "/suggestions",
            data={
                "action": "add_to_queue",
                "audiobook_id": "ab-1",
                "ebook_filename": "suggestions-original.epub",
                "ebook_display_name": "Suggestions No Cache",
                "storyteller_uuid": "story-uuid-suggestions-nocache",
            },
        )
        self.assertEqual(add_response.status_code, 302)

        process_response = self.client.post(
            "/suggestions",
            data={"action": "process_queue"},
        )
        self.assertEqual(process_response.status_code, 302)

        self.mock_container.mock_storyteller_client.download_book.assert_not_called()
        self.mock_container.mock_database_service.save_book.assert_called_once()
        saved_book = self.mock_container.mock_database_service.save_book.call_args[0][0]
        self.assertEqual(saved_book.ebook_filename, "suggestions-original.epub")
        self.assertEqual(saved_book.original_ebook_filename, "suggestions-original.epub")
        self.assertEqual(saved_book.storyteller_uuid, "story-uuid-suggestions-nocache")


if __name__ == "__main__":
    unittest.main(verbosity=2)
