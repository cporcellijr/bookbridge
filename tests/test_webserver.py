"""
Proper Flask Integration Test with Dependency Injection.
No patches needed - clean dependency injection pattern.
"""

import unittest
import tempfile
import os
import json
from pathlib import Path
from unittest.mock import Mock, patch
import sys

# Add project root to Python path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.utils.kosync_headers import KOSYNC_ACCEPT, hash_kosync_key


def _http_response(status_code, payload=None, text=""):
    response = Mock()
    response.status_code = status_code
    response.text = text
    response.json.return_value = payload or {}
    return response


class MockContainer:
    """Mock container for testing - implements the same interface as real container."""

    def __init__(self):
        self.mock_sync_manager = Mock()
        self.mock_abs_client = Mock()
        self.mock_booklore_client = Mock()
        self.mock_storyteller_client = Mock()
        self.mock_database_service = Mock()
        self.mock_database_service.get_all_settings.return_value = {}  # Default empty settings
        self.mock_ebook_parser = Mock()
        self.mock_sync_clients = Mock()
        self.mock_forge_service = Mock()
        self.mock_forge_service.active_tasks = set()

        # Configure the sync manager to return our mock clients
        self.mock_sync_manager.abs_client = self.mock_abs_client
        self.mock_sync_manager.booklore_client = self.mock_booklore_client
        self.mock_sync_manager.storyteller_client = self.mock_storyteller_client
        self.mock_sync_manager.get_abs_title.return_value = 'Test Book Title'
        self.mock_sync_manager.get_duration.return_value = 3600
        self.mock_sync_manager.clear_progress = Mock()

    def sync_manager(self):
        return self.mock_sync_manager

    def abs_client(self):
        return self.mock_abs_client

    def booklore_client(self):
        return self.mock_booklore_client

    def storyteller_client(self):
        return self.mock_storyteller_client

    def ebook_parser(self):
        return self.mock_ebook_parser

    def database_service(self):
        return self.mock_database_service

    def forge_service(self):
        return self.mock_forge_service

    def sync_clients(self):
        """Return mock sync clients for integrations."""
        return {
            'ABS': Mock(is_configured=Mock(return_value=True)),
            'KoSync': Mock(is_configured=Mock(return_value=True)),
            'Storyteller': Mock(is_configured=Mock(return_value=False))
        }

    def data_dir(self):
        return Path(tempfile.gettempdir()) / 'test_data'

    def books_dir(self):
        return Path(tempfile.gettempdir()) / 'test_books'

    def epub_cache_dir(self):
        return Path(tempfile.gettempdir()) / 'test_epub_cache'

    def sync_clients(self):
        return self.mock_sync_clients


class CleanFlaskIntegrationTest(unittest.TestCase):
    """Clean Flask integration test using proper dependency injection."""

    def setUp(self):
        """Set up test environment with mocked dependencies."""
        # Create temporary directory for test
        self.temp_dir = tempfile.mkdtemp()

        # Set up environment variables for testing
        os.environ['DATA_DIR'] = self.temp_dir
        os.environ['BOOKS_DIR'] = self.temp_dir

        # Create mock container
        self.mock_container = MockContainer()

        # Mock the database initialization function
        def mock_initialize_database(data_dir):
            return self.mock_container.mock_database_service

        # Patch the initialize_database import BEFORE importing web_server
        import src.db.migration_utils
        self.original_init_db = src.db.migration_utils.initialize_database
        src.db.migration_utils.initialize_database = mock_initialize_database

        # Use the app factory to get a fresh app instance for each test
        from src.web_server import create_app, setup_dependencies
        self.app, _ = create_app(test_container=self.mock_container)
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()

        # Store references for easy access
        self.mock_manager = self.mock_container.mock_sync_manager
        self.mock_abs_client = self.mock_container.mock_abs_client
        self.mock_booklore_client = self.mock_container.mock_booklore_client
        self.mock_storyteller_client = self.mock_container.mock_storyteller_client
        self.mock_database_service = self.mock_container.mock_database_service

    def tearDown(self):
        """Clean up after test."""
        # Restore original function
        import src.db.migration_utils
        src.db.migration_utils.initialize_database = self.original_init_db

        # Clean up temp directory
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _prepare_storyteller_assets(self, title: str, chapter_count: int = 2):
        assets_root = Path(self.temp_dir) / "storyteller_assets"
        transcriptions_dir = assets_root / "assets" / title / "transcriptions"
        transcriptions_dir.mkdir(parents=True, exist_ok=True)
        for idx in range(chapter_count):
            filename = f"{idx + 1:05d}-00001.json"
            payload = {"transcript": f"chapter {idx + 1}", "wordTimeline": []}
            (transcriptions_dir / filename).write_text(json.dumps(payload), encoding="utf-8")
        os.environ["STORYTELLER_ASSETS_DIR"] = str(assets_root)
        self.addCleanup(lambda: os.environ.pop("STORYTELLER_ASSETS_DIR", None))

    def test_dependency_injection_works(self):
        """Verify that dependency injection is working properly."""
        from src.web_server import manager, database_service, container

        # Verify our mocked dependencies are injected
        self.assertIs(container, self.mock_container)
        self.assertIs(manager, self.mock_container.mock_sync_manager)
        self.assertIs(database_service, self.mock_container.mock_database_service)

        print("[OK] Dependency injection working correctly")

    def test_index_endpoint_with_mocked_dependencies(self):
        """Test index endpoint using clean dependency injection."""
        # Setup mock data
        from src.db.models import Book
        test_book = Book(
            abs_id='test-book-123',
            abs_title='Test Book',
            ebook_filename='test.epub',
            kosync_doc_id='test-doc-id',
            status='active',
            duration=3600  # Add duration for progress calculation
        )

        # Create mock states with different progress values
        from src.db.models import State
        mock_states = [
            State(
                abs_id='test-book-123',
                client_name='kosync',
                last_updated=1642291200,
                percentage=0.45,  # 45% progress
                xpath='/html/body/div[2]/p[5]'
            ),
            State(
                abs_id='test-book-123',
                client_name='storyteller',
                last_updated=1642291300,
                percentage=0.42,  # 42% progress
                cfi='epubcfi(/6/4[chapter01]!/4/2/2[para05]/1:0)'
            ),
            State(
                abs_id='test-book-123',
                client_name='abs',
                last_updated=1642291100,
                percentage=0.44,  # 44% progress
                timestamp=1584  # 44% of 3600 seconds duration
            ),
            State(
                abs_id='test-book-123',
                client_name='booklore',
                last_updated=1642291150,
                percentage=0.40,  # 40% progress
                cfi='epubcfi(/6/6[chapter02]!/4/1/1:0)'
            )
        ]

        self.mock_database_service.get_all_books.return_value = [test_book]
        self.mock_database_service.get_states_for_book.return_value = mock_states
        self.mock_database_service.get_all_states.return_value = mock_states
        self.mock_database_service.get_hardcover_details.return_value = None
        self.mock_database_service.get_all_hardcover_details.return_value = []
        self.mock_database_service.get_all_pending_suggestions.return_value = []

        # Mock the sync_clients call for integrations
        # Mock the sync_clients call for integrations
        # Mock the sync_clients call for integrations
        # Since container.sync_clients() returns the mock object, we need to mock .items()
        clients_dict = {
                'ABS': Mock(is_configured=Mock(return_value=True)),
                'KoSync': Mock(is_configured=Mock(return_value=True)),
                'Storyteller': Mock(is_configured=Mock(return_value=False))
        }
        self.mock_container.mock_sync_clients.items.return_value = clients_dict.items()

        # Mock render_template to capture arguments
        import src.web_server
        original_render = src.web_server.render_template
        mock_render = Mock(return_value="Mocked HTML Response")
        src.web_server.render_template = mock_render

        try:
            # Make HTTP request
            response = self.client.get('/')

            # Verify response
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, b"Mocked HTML Response")

            # Verify database was called
            self.mock_database_service.get_all_books.assert_called_once()
            self.mock_database_service.get_all_states.assert_called_once()
            self.mock_database_service.get_all_hardcover_details.assert_called_once()

            # Verify render_template was called with correct arguments
            mock_render.assert_called_once()
            render_args, render_kwargs = mock_render.call_args

            # Check template name
            self.assertEqual(render_args[0], 'index.html')

            # Check required template variables
            self.assertIn('mappings', render_kwargs)
            self.assertIn('integrations', render_kwargs)
            self.assertIn('progress', render_kwargs)

            # Verify mappings data structure
            mappings = render_kwargs['mappings']
            self.assertEqual(len(mappings), 1)
            mapping = mappings[0]

            # Check mapping contains expected book data
            self.assertEqual(mapping['abs_id'], 'test-book-123')
            self.assertEqual(mapping['abs_title'], 'Test Book')
            self.assertEqual(mapping['ebook_filename'], 'test.epub')
            self.assertEqual(mapping['status'], 'active')

            # Check progress values based on mock states
            # The unified progress should be the maximum of all client progress values
            self.assertEqual(mapping['unified_progress'], 45.0)  # Max of 45%, 42%, 44%, 40%

            # Check that states structure is present and contains expected data
            self.assertIn('states', mapping)
            states = mapping['states']

            # Verify each client state is stored correctly
            self.assertIn('kosync', states)
            self.assertEqual(states['kosync']['percentage'], 45.0)  # 45% from mock state
            self.assertEqual(states['kosync']['timestamp'], 0)
            self.assertEqual(states['kosync']['last_updated'], 1642291200)

            self.assertIn('storyteller', states)
            self.assertEqual(states['storyteller']['percentage'], 42.0)  # 42% from mock state
            self.assertEqual(states['storyteller']['timestamp'], 0)
            self.assertEqual(states['storyteller']['last_updated'], 1642291300)

            self.assertIn('booklore', states)
            self.assertEqual(states['booklore']['percentage'], 40.0)  # 40% from mock state
            self.assertEqual(states['booklore']['timestamp'], 0)
            self.assertEqual(states['booklore']['last_updated'], 1642291150)

            self.assertIn('abs', states)
            self.assertEqual(states['abs']['percentage'], 44.0)  # 44% from mock state
            self.assertEqual(states['abs']['timestamp'], 1584)  # Timestamp from mock state
            self.assertEqual(states['abs']['last_updated'], 1642291100)

            # Hardcover should not be present since no hardcover states were provided
            self.assertNotIn('hardcover', states)

            # Check hardcover fields are properly initialized
            self.assertFalse(mapping['hardcover_linked'])
            self.assertIsNone(mapping['hardcover_book_id'])
            self.assertIsNone(mapping['hardcover_title'])

            # Verify integrations data
            integrations = render_kwargs['integrations']
            self.assertTrue(integrations.get('abs', False))  # Mocked as True
            self.assertTrue(integrations.get('kosync', False))  # Mocked as True
            self.assertFalse(integrations.get('storyteller', True))  # Mocked as False

            # Verify overall progress (should be calculated from book progress and duration)
            overall_progress = render_kwargs['progress']
            # With duration=3600 and unified_progress=45%, the calculation should reflect this
            self.assertGreater(overall_progress, 0)  # Should be > 0 now that we have progress data
            self.assertLessEqual(overall_progress, 100)  # Should be a valid percentage

            print("[OK] Index endpoint test passed with correct response verification")

        finally:
            src.web_server.render_template = original_render

    def test_api_status_endpoint_clean_di(self):
        """Test API status endpoint with clean dependency injection."""
        # Setup mock data
        from src.db.models import Book
        test_book = Book(
            abs_id='api-test-book-123',
            abs_title='API Test Book',
            ebook_filename='api-test.epub',
            kosync_doc_id='api-test-doc-id',
            status='active',
            duration=3600
        )

        self.mock_database_service.get_all_books.return_value = [test_book]
        self.mock_database_service.get_states_for_book.return_value = []

        # Make HTTP request
        response = self.client.get('/api/status')

        # Verify response
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content_type, 'application/json')

        data = response.get_json()
        self.assertIn('mappings', data)
        self.assertEqual(len(data['mappings']), 1)
        self.assertEqual(data['mappings'][0]['abs_id'], 'api-test-book-123')
        
        # Verify percentage scaling (should be 0 because states mock returned empty list)
        # But let's verify structure
        self.assertIn('states', data['mappings'][0])

        print("[OK] API status endpoint test passed with clean DI")

    def test_api_status_percentage_scaling(self):
        """Test that API status scales percentages correctly (0.45 -> 45.0)."""
        # Setup mock data
        from src.db.models import Book, State
        test_book = Book(
            abs_id='scale-test-123',
            abs_title='Scale Test',
            ebook_filename='scale.epub',
            kosync_doc_id='scale-doc',
            status='active'
        )

        # Mock states with decimal percentages
        mock_states = [
            State(
                abs_id='scale-test-123',
                client_name='kosync',
                percentage=0.455,  # Should become 45.5
                last_updated=1000
            ),
            State(
                abs_id='scale-test-123',
                client_name='storyteller',
                percentage=0.1,    # Should become 10.0
                last_updated=2000
            )
        ]

        self.mock_database_service.get_all_books.return_value = [test_book]
        self.mock_database_service.get_states_for_book.return_value = mock_states
        self.mock_database_service.get_all_states.return_value = mock_states

        # Make HTTP request
        response = self.client.get('/api/status')
        data = response.get_json()

        # Verify mappings
        mapping = data['mappings'][0]
        
        # Check nested states
        self.assertEqual(mapping['states']['kosync']['percentage'], 45.5)
        self.assertEqual(mapping['states']['storyteller']['percentage'], 10.0)

        # Check legacy flat fields
        self.assertEqual(mapping['kosync_pct'], 45.5)
        self.assertEqual(mapping['storyteller_pct'], 10.0)

        print("[OK] API status percentage scaling test passed")

    def test_match_endpoint_with_clean_di(self):
        """Test match endpoint using clean dependency injection."""
        # Mock the kosync ID generation
        import src.web_server
        original_get_kosync = src.web_server.get_kosync_id_for_ebook
        src.web_server.get_kosync_id_for_ebook = Mock(return_value='test-kosync-id')

        try:
            # Configure mocks
            self.mock_abs_client.get_all_audiobooks.return_value = [
                {
                    'id': 'test-audiobook-123',
                    'media': {
                        'metadata': {'title': 'Test Book'},
                        'duration': 3600
                    }
                }
            ]
            self.mock_booklore_client.is_configured.return_value = True
            self.mock_booklore_client.find_book_by_filename.return_value = {'id': 'book-123'}

            # Configure client methods
            self.mock_abs_client.add_to_collection.return_value = True
            self.mock_booklore_client.add_to_shelf.return_value = True
            self.mock_storyteller_client.add_to_collection.return_value = True

            # Configure get_book_by_kosync_id to return None (no existing book to merge)
            self.mock_database_service.get_book_by_kosync_id.return_value = None
            self.mock_database_service.get_book.return_value = None
            
            # Make HTTP POST request
            response = self.client.post('/match', data={
                'audiobook_id': 'test-audiobook-123',
                'ebook_filename': 'test-book.epub'
            })

            # Verify response
            self.assertEqual(response.status_code, 302)
            self.assertTrue(response.location.endswith('/'))

            # Verify service interactions
            self.mock_database_service.save_book.assert_called_once()

            # Verify save_book was called with correct arguments
            save_book_call_args = self.mock_database_service.save_book.call_args
            saved_book = save_book_call_args[0][0]  # First positional argument

            # Verify the Book object has correct attributes
            self.assertEqual(saved_book.abs_id, 'test-audiobook-123')
            self.assertEqual(saved_book.abs_title, 'Test Book Title')  # From mock manager
            self.assertEqual(saved_book.ebook_filename, 'test-book.epub')
            self.assertEqual(saved_book.kosync_doc_id, 'test-kosync-id')
            self.assertEqual(saved_book.status, 'pending')
            self.assertEqual(saved_book.duration, 3600)
            self.assertIsNone(saved_book.transcript_file)

            self.mock_abs_client.add_to_collection.assert_called_once_with('test-audiobook-123', 'Synced with KOReader')
            self.mock_booklore_client.add_to_shelf.assert_called_once_with('test-book.epub', 'Kobo')
            self.mock_storyteller_client.add_to_collection.assert_not_called()

            print("[OK] Match endpoint test passed with clean DI")

        finally:
            src.web_server.get_kosync_id_for_ebook = original_get_kosync

    def test_storyteller_unlink_removes_from_collection_by_uuid(self):
        """Unlinking Storyteller should remove the prior UUID from Storyteller collection."""
        from src.db.models import Book

        test_book = Book(
            abs_id='st-unlink-1',
            abs_title='Story Book',
            ebook_filename='storyteller_uuid-1.epub',
            original_ebook_filename='original.epub',
            storyteller_uuid='uuid-1',
            status='active'
        )
        self.mock_database_service.get_book.return_value = test_book
        self.mock_storyteller_client.remove_from_collection_by_uuid.return_value = True

        response = self.client.post('/api/storyteller/link/st-unlink-1', json={'uuid': 'none'})

        self.assertEqual(response.status_code, 200)
        self.mock_storyteller_client.remove_from_collection_by_uuid.assert_called_once_with(
            'uuid-1',
            'Synced with KOReader'
        )
        self.mock_database_service.save_book.assert_called_once()

    def test_delete_mapping_removes_storyteller_collection_by_uuid(self):
        """Deleting a mapping should remove Storyteller UUID from collection when linked."""
        from src.db.models import Book

        test_book = Book(
            abs_id='delete-st-1',
            abs_title='Delete Story Book',
            ebook_filename='storyteller_uuid-del.epub',
            storyteller_uuid='uuid-del',
            status='active'
        )
        self.mock_database_service.get_book.return_value = test_book
        self.mock_storyteller_client.remove_from_collection_by_uuid.return_value = True
        self.mock_booklore_client.is_configured.return_value = False
        self.mock_manager.epub_cache_dir = None

        response = self.client.post('/delete/delete-st-1')

        self.assertEqual(response.status_code, 302)
        self.mock_storyteller_client.remove_from_collection_by_uuid.assert_called_once_with(
            'uuid-del',
            'Synced with KOReader'
        )
        self.mock_database_service.delete_book.assert_called_once_with('delete-st-1')

    def test_delete_mapping_infers_storyteller_uuid_from_filename(self):
        """Deleting a mapping should infer Storyteller UUID from filename when DB UUID is missing."""
        from src.db.models import Book

        inferred_uuid = 'bbe93e33-6b8d-4368-95a0-c357be1fa230'
        test_book = Book(
            abs_id='delete-st-2',
            abs_title='Delete Story Book 2',
            ebook_filename=f'storyteller_{inferred_uuid}.epub',
            storyteller_uuid=None,
            status='active'
        )
        self.mock_database_service.get_book.return_value = test_book
        self.mock_storyteller_client.remove_from_collection_by_uuid.return_value = True
        self.mock_booklore_client.is_configured.return_value = False
        self.mock_manager.epub_cache_dir = None

        response = self.client.post('/delete/delete-st-2')

        self.assertEqual(response.status_code, 302)
        self.mock_storyteller_client.remove_from_collection_by_uuid.assert_called_once_with(
            inferred_uuid,
            'Synced with KOReader'
        )
        self.mock_database_service.delete_book.assert_called_once_with('delete-st-2')

    @patch('src.web_server.ingest_storyteller_transcripts', return_value=None)
    def test_api_storyteller_link_preserves_storyteller_source_when_ingest_missing(self, _mock_ingest):
        from src.db.models import Book

        test_book = Book(
            abs_id='story-link-1',
            abs_title='Story Link',
            ebook_filename='original.epub',
            storyteller_uuid=None,
            transcript_source=None,
            transcript_file=None,
            status='active'
        )
        self.mock_database_service.get_book.return_value = test_book
        self.mock_storyteller_client.download_book.return_value = True
        self.mock_abs_client.get_item_details.return_value = {
            'media': {'chapters': [{'start': 0.0, 'end': 10.0}]}
        }

        response = self.client.post('/api/storyteller/link/story-link-1', json={'uuid': 'uuid-123'})

        self.assertEqual(response.status_code, 200)
        self.mock_database_service.save_book.assert_called_once()
        saved_book = self.mock_database_service.save_book.call_args[0][0]
        self.assertEqual(saved_book.storyteller_uuid, 'uuid-123')
        self.assertEqual(saved_book.transcript_source, 'storyteller')
        self.assertIsNone(saved_book.transcript_file)
        self.mock_database_service.dismiss_suggestion.assert_called_once_with('story-link-1')

    def test_api_storyteller_link_real_ingest_persists_manifest(self):
        from src.db.models import Book

        self._prepare_storyteller_assets("Story Link", chapter_count=2)

        test_book = Book(
            abs_id='story-link-real',
            abs_title='Story Link',
            ebook_filename='original.epub',
            storyteller_uuid=None,
            transcript_source=None,
            transcript_file=None,
            status='active'
        )
        self.mock_database_service.get_book.return_value = test_book
        self.mock_storyteller_client.download_book.return_value = True
        self.mock_abs_client.get_item_details.return_value = {
            'media': {
                'chapters': [
                    {'start': 0.0, 'end': 10.0},
                    {'start': 10.0, 'end': 20.0},
                ]
            }
        }

        response = self.client.post('/api/storyteller/link/story-link-real', json={'uuid': 'uuid-real'})

        self.assertEqual(response.status_code, 200)
        self.mock_database_service.save_book.assert_called_once()
        saved_book = self.mock_database_service.save_book.call_args[0][0]
        self.assertEqual(saved_book.storyteller_uuid, 'uuid-real')
        self.assertEqual(saved_book.transcript_source, 'storyteller')
        self.assertIsNotNone(saved_book.transcript_file)
        self.assertTrue(Path(saved_book.transcript_file).exists())

    @patch('src.web_server.ingest_storyteller_transcripts', return_value='/tmp/storyteller-manifest.json')
    @patch('src.web_server.get_kosync_id_for_ebook', return_value='hash-ebook-only-link-1')
    def test_api_storyteller_link_ebook_only_skips_abs_chapter_lookup(self, _mock_kosync, _mock_ingest):
        from src.db.models import Book

        test_book = Book(
            abs_id='ebook-link-1',
            abs_title='Ebook Link',
            ebook_filename='ebook-link.epub',
            kosync_doc_id='hash-existing',
            sync_mode='ebook_only',
            status='active',
        )
        self.mock_database_service.get_book.return_value = test_book
        self.mock_storyteller_client.download_book.return_value = True

        response = self.client.post('/api/storyteller/link/ebook-link-1', json={'uuid': 'uuid-ebook-only'})

        self.assertEqual(response.status_code, 200)
        self.mock_abs_client.get_item_details.assert_not_called()
        self.mock_database_service.save_book.assert_called_once()
        saved_book = self.mock_database_service.save_book.call_args[0][0]
        self.assertEqual(saved_book.sync_mode, 'ebook_only')
        self.assertEqual(saved_book.storyteller_uuid, 'uuid-ebook-only')
        self.assertEqual(saved_book.transcript_source, 'storyteller')

    def test_index_endpoint_ebook_only_uses_cached_ebook_metadata_for_display(self):
        from src.db.models import Book, BookloreBook

        test_book = Book(
            abs_id='ebook-only-1',
            abs_title='book-file',
            ebook_filename='book-file.epub',
            sync_mode='ebook_only',
            status='active'
        )
        cached_book = BookloreBook(
            filename='book-file.epub',
            title='Displayed Title',
            authors='Displayed Author',
            raw_metadata=json.dumps({
                'title': 'Displayed Title',
                'subtitle': 'Displayed Subtitle',
                'authors': 'Displayed Author'
            })
        )

        self.mock_database_service.get_all_books.return_value = [test_book]
        self.mock_database_service.get_all_states.return_value = []
        self.mock_database_service.get_all_hardcover_details.return_value = []
        self.mock_database_service.get_all_pending_suggestions.return_value = []
        self.mock_database_service.get_booklore_book.side_effect = lambda filename: cached_book if filename == 'book-file.epub' else None
        self.mock_booklore_client.is_configured.return_value = False

        clients_dict = {
            'ABS': Mock(is_configured=Mock(return_value=True)),
            'KoSync': Mock(is_configured=Mock(return_value=True)),
            'Storyteller': Mock(is_configured=Mock(return_value=False))
        }
        self.mock_container.mock_sync_clients.items.return_value = clients_dict.items()

        import src.web_server
        original_render = src.web_server.render_template
        mock_render = Mock(return_value="Mocked HTML Response")
        src.web_server.render_template = mock_render

        try:
            response = self.client.get('/')
            self.assertEqual(response.status_code, 200)
            mapping = mock_render.call_args.kwargs['mappings'][0]
            self.assertEqual(mapping['abs_title'], 'Displayed Title')
            self.assertEqual(mapping['abs_subtitle'], 'Displayed Subtitle')
            self.assertEqual(mapping['abs_author'], 'Displayed Author')
        finally:
            src.web_server.render_template = original_render

    def test_index_endpoint_storyteller_uses_storyteller_metadata_for_display(self):
        from src.db.models import Book

        test_book = Book(
            abs_id='ebook-storyteller-1',
            abs_title='storyteller_uuid-book',
            ebook_filename='storyteller_uuid-book.epub',
            storyteller_uuid='uuid-story-1',
            sync_mode='ebook_only',
            status='active'
        )

        self.mock_database_service.get_all_books.return_value = [test_book]
        self.mock_database_service.get_all_states.return_value = []
        self.mock_database_service.get_all_hardcover_details.return_value = []
        self.mock_database_service.get_all_pending_suggestions.return_value = []
        self.mock_database_service.get_booklore_book.return_value = None
        self.mock_booklore_client.is_configured.return_value = False
        self.mock_storyteller_client.is_configured.return_value = True
        self.mock_storyteller_client.get_book_details.return_value = {
            'title': 'Storyteller Title',
            'subtitle': 'Storyteller Subtitle',
            'authors': [{'name': 'Storyteller Author'}]
        }

        clients_dict = {
            'ABS': Mock(is_configured=Mock(return_value=True)),
            'KoSync': Mock(is_configured=Mock(return_value=True)),
            'Storyteller': Mock(is_configured=Mock(return_value=True))
        }
        self.mock_container.mock_sync_clients.items.return_value = clients_dict.items()

        import src.web_server
        original_render = src.web_server.render_template
        mock_render = Mock(return_value="Mocked HTML Response")
        src.web_server.render_template = mock_render

        try:
            response = self.client.get('/')
            self.assertEqual(response.status_code, 200)
            mapping = mock_render.call_args.kwargs['mappings'][0]
            self.assertEqual(mapping['abs_title'], 'Storyteller Title')
            self.assertEqual(mapping['abs_subtitle'], 'Storyteller Subtitle')
            self.assertEqual(mapping['abs_author'], 'Storyteller Author')
        finally:
            src.web_server.render_template = original_render

    def test_clear_progress_endpoint_clean_di(self):
        """Test clear progress endpoint with clean dependency injection."""
        # Setup mock book
        from src.db.models import Book
        test_book = Book(
            abs_id='clear-test-book',
            abs_title='Clear Test Book',
            ebook_filename='clear-test.epub',
            kosync_doc_id='clear-test-doc-id',
            status='active'
        )

        self.mock_database_service.get_book.return_value = test_book

        # Make HTTP request
        response = self.client.post('/clear-progress/clear-test-book')

        # Verify response
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith('/'))

        # Verify clear_progress was called on manager
        self.mock_manager.clear_progress.assert_called_once_with('clear-test-book')

        print("[OK] Clear progress endpoint test passed with clean DI")

    def test_settings_endpoint_clean_di(self):
        """Test settings endpoint with clean dependency injection."""
        # Mock database settings
        self.mock_database_service.get_all_settings.return_value = {
            'KOSYNC_ENABLED': 'true',
            'SYNC_PERIOD_MINS': '10'
        }

        # Mock render_template
        import src.web_server
        original_render = src.web_server.render_template
        mock_render = Mock(return_value="Settings Page HTML")
        src.web_server.render_template = mock_render

        try:
            # Make HTTP request
            response = self.client.get('/settings')

            # Verify response
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, b"Settings Page HTML")

            # Verify database was called to load settings
            # Note: settings() function calls database_service.get_all_settings() implicitly 
            # via ConfigLoader or os.environ?
            # Actually, looking at the code, settings() calls database_service.get_all_settings() 
            # only on POST. On GET it just renders template.
            # But the template rendering uses `get_val` helper which reads from os.environ.
            # So we just verify it renders successfully.
            
            mock_render.assert_called_once()
            args, _ = mock_render.call_args
            self.assertEqual(args[0], 'settings.html')

            print("[OK] Settings endpoint test passed")

        finally:
            src.web_server.render_template = original_render

    def test_shelfmark_redirects_to_configured_external_url(self):
        with patch.dict(os.environ, {'SHELFMARK_URL': 'shelfmark.blackcatmedia.xyz'}, clear=False):
            response = self.client.get('/shelfmark')

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers['Location'], 'http://shelfmark.blackcatmedia.xyz')

    def test_shelfmark_redirects_to_index_when_not_configured(self):
        with patch.dict(os.environ, {'SHELFMARK_URL': ''}, clear=False):
            response = self.client.get('/shelfmark')

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers['Location'].endswith('/'))

    def test_api_health_endpoint(self):
        response = self.client.get('/api/health')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['ok'], True)
        self.assertIn('no-store', response.headers.get('Cache-Control', ''))

    @patch('src.web_server.start_restart_async')
    def test_api_restart_endpoint(self, mock_start_restart_async):
        response = self.client.post('/api/restart')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['ok'], True)
        self.assertIn('no-store', response.headers.get('Cache-Control', ''))
        mock_start_restart_async.assert_called_once()

    @patch('src.web_server.requests.get')
    def test_test_connection_abs_uses_post_payload_not_saved_env(self, mock_get):
        def fake_get(url, headers=None, timeout=None):
            self.assertEqual(url, 'http://typed-abs/api/me')
            self.assertEqual(headers, {'Authorization': 'Bearer wrong-token'})
            self.assertEqual(timeout, 10)
            return _http_response(403)

        mock_get.side_effect = fake_get

        with patch.dict(os.environ, {'ABS_SERVER': 'http://saved-abs', 'ABS_KEY': 'saved-token'}, clear=False):
            response = self.client.post(
                '/api/test-connection/abs',
                json={'ABS_SERVER': 'typed-abs', 'ABS_KEY': 'wrong-token'},
            )

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertFalse(data['ok'])
        self.assertIn('Authentication failed', data['message'])
        mock_get.assert_called_once()

    @patch('src.web_server.requests.get')
    def test_test_connection_kosync_fails_when_auth_fails(self, mock_get):
        def fake_get(url, headers=None, timeout=None):
            if url == 'http://typed-kosync/healthcheck':
                self.assertEqual(headers['x-auth-user'], 'reader')
                self.assertEqual(headers['x-auth-key'], hash_kosync_key('wrong-pass'))
                self.assertEqual(headers['accept'], KOSYNC_ACCEPT)
                self.assertEqual(timeout, 5)
                return _http_response(200)
            if url == 'http://typed-kosync/users/auth':
                self.assertEqual(headers['x-auth-user'], 'reader')
                self.assertEqual(headers['x-auth-key'], hash_kosync_key('wrong-pass'))
                self.assertEqual(headers['accept'], KOSYNC_ACCEPT)
                self.assertEqual(timeout, 5)
                return _http_response(401)
            raise AssertionError(f'Unexpected URL {url}')

        mock_get.side_effect = fake_get

        response = self.client.post(
            '/api/test-connection/kosync',
            json={
                'KOSYNC_ENABLED': True,
                'KOSYNC_SERVER': 'typed-kosync',
                'KOSYNC_USER': 'reader',
                'KOSYNC_KEY': 'wrong-pass',
            },
        )

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertFalse(data['ok'])
        self.assertIn('Authentication failed', data['message'])
        self.assertEqual(mock_get.call_count, 2)

    @patch('src.web_server.requests.get')
    def test_test_connection_kosync_succeeds_after_healthcheck_and_auth(self, mock_get):
        def fake_get(url, headers=None, timeout=None):
            if url == 'http://typed-kosync/healthcheck':
                self.assertEqual(headers['x-auth-user'], 'reader')
                self.assertEqual(headers['x-auth-key'], hash_kosync_key('good-pass'))
                self.assertEqual(headers['accept'], KOSYNC_ACCEPT)
                return _http_response(200)
            if url == 'http://typed-kosync/users/auth':
                self.assertEqual(headers['x-auth-user'], 'reader')
                self.assertEqual(headers['x-auth-key'], hash_kosync_key('good-pass'))
                self.assertEqual(headers['accept'], KOSYNC_ACCEPT)
                return _http_response(200)
            raise AssertionError(f'Unexpected URL {url}')

        mock_get.side_effect = fake_get

        response = self.client.post(
            '/api/test-connection/kosync',
            json={
                'KOSYNC_ENABLED': True,
                'KOSYNC_SERVER': 'typed-kosync',
                'KOSYNC_USER': 'reader',
                'KOSYNC_KEY': 'good-pass',
            },
        )

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data['ok'])
        self.assertIn('credentials are valid', data['message'])
        self.assertEqual(mock_get.call_count, 2)

    @patch('src.web_server.requests.get')
    def test_test_connection_kosync_auth_success_overrides_healthcheck_403(self, mock_get):
        def fake_get(url, headers=None, timeout=None):
            self.assertEqual(headers['x-auth-user'], 'reader')
            self.assertEqual(headers['x-auth-key'], hash_kosync_key('good-pass'))
            self.assertEqual(headers['accept'], KOSYNC_ACCEPT)
            if url == 'http://typed-kosync/healthcheck':
                return _http_response(403)
            if url == 'http://typed-kosync/users/auth':
                return _http_response(200)
            raise AssertionError(f'Unexpected URL {url}')

        mock_get.side_effect = fake_get

        response = self.client.post(
            '/api/test-connection/kosync',
            json={
                'KOSYNC_ENABLED': True,
                'KOSYNC_SERVER': 'typed-kosync',
                'KOSYNC_USER': 'reader',
                'KOSYNC_KEY': 'good-pass',
            },
        )

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data['ok'])
        self.assertIn('healthcheck returned 403', data['message'])
        self.assertEqual(mock_get.call_count, 2)

    @patch('src.web_server.requests.post')
    def test_test_connection_hardcover_accepts_list_shaped_me_payload(self, mock_post):
        mock_post.return_value = _http_response(
            200,
            payload={'data': {'me': [{'id': 1, 'username': 'reader'}]}},
        )

        response = self.client.post(
            '/api/test-connection/hardcover',
            json={
                'HARDCOVER_ENABLED': True,
                'HARDCOVER_TOKEN': 'good-token',
            },
        )

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data['ok'])
        self.assertIn("Connected as 'reader'", data['message'])

    @patch('src.web_server.requests.post')
    def test_test_connection_hardcover_invalid_token(self, mock_post):
        mock_post.return_value = _http_response(403)

        response = self.client.post(
            '/api/test-connection/hardcover',
            json={
                'HARDCOVER_ENABLED': True,
                'HARDCOVER_TOKEN': 'bad-token',
            },
        )

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertFalse(data['ok'])
        self.assertEqual(data['message'], 'Invalid API token')

    @patch('src.web_server.requests.post')
    def test_test_connection_storyteller_uses_post_payload_not_saved_env(self, mock_post):
        def fake_post(url, data=None, headers=None, timeout=None):
            self.assertEqual(url, 'http://typed-storyteller/api/token')
            self.assertEqual(data, {'username': 'typed-user', 'password': 'wrong-pass'})
            self.assertEqual(headers, {'Content-Type': 'application/x-www-form-urlencoded'})
            self.assertEqual(timeout, 10)
            return _http_response(401)

        mock_post.side_effect = fake_post

        with patch.dict(os.environ, {
            'STORYTELLER_ENABLED': 'true',
            'STORYTELLER_API_URL': 'http://saved-storyteller',
            'STORYTELLER_USER': 'saved-user',
            'STORYTELLER_PASSWORD': 'saved-pass',
        }, clear=False):
            response = self.client.post(
                '/api/test-connection/storyteller',
                json={
                    'STORYTELLER_ENABLED': True,
                    'STORYTELLER_API_URL': 'typed-storyteller',
                    'STORYTELLER_USER': 'typed-user',
                    'STORYTELLER_PASSWORD': 'wrong-pass',
                },
            )

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertFalse(data['ok'])
        self.assertIn('Invalid username or password', data['message'])
        mock_post.assert_called_once()

    @patch('src.web_server.requests.post')
    def test_test_connection_storyteller_respects_payload_disabled_flag(self, mock_post):
        with patch.dict(os.environ, {'STORYTELLER_ENABLED': 'true'}, clear=False):
            response = self.client.post(
                '/api/test-connection/storyteller',
                json={
                    'STORYTELLER_ENABLED': False,
                    'STORYTELLER_API_URL': 'typed-storyteller',
                    'STORYTELLER_USER': 'typed-user',
                    'STORYTELLER_PASSWORD': 'typed-pass',
                },
            )

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertFalse(data['ok'])
        self.assertEqual(data['message'], 'Storyteller is disabled')
        mock_post.assert_not_called()

    def test_clear_stale_suggestions_api(self):
        """Test the clear-stale-suggestions API endpoint."""
        # Setup mock return value
        self.mock_database_service.clear_stale_suggestions.return_value = 5
        
        # Make POST request
        response = self.client.post('/api/suggestions/clear_stale')
        
        # Verify response
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data['success'])
        self.assertEqual(data['count'], 5)
        
        # Verify service call
        self.mock_database_service.clear_stale_suggestions.assert_called_once()
        
        print("[OK] Clear stale suggestions API test passed")


class FindEbookFileTest(unittest.TestCase):
    """Test find_ebook_file function handles special characters in filenames."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        os.environ["BOOKS_DIR"] = self.temp_dir

    def tearDown(self):
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_find_ebook_file_with_brackets(self):
        """Test that filenames with brackets like [01] are found correctly."""
        from src.web_server import find_ebook_file
        import src.web_server

        src.web_server.EBOOK_DIR = Path(self.temp_dir)

        filename = "Hyperion Cantos [02] - The Fall of Hyperion.epub"
        test_file = Path(self.temp_dir) / filename
        test_file.touch()

        result = find_ebook_file(filename)
        self.assertIsNotNone(result)
        self.assertEqual(result.name, filename)

    @unittest.skipIf(os.name == 'nt', "Windows does not support * in filenames")
    def test_find_ebook_file_with_asterisk(self):
        """Test that filenames with asterisks are found correctly."""
        from src.web_server import find_ebook_file
        import src.web_server

        src.web_server.EBOOK_DIR = Path(self.temp_dir)

        filename = "Book Title * Special Edition.epub"
        test_file = Path(self.temp_dir) / filename
        test_file.touch()

        result = find_ebook_file(filename)
        self.assertIsNotNone(result)
        self.assertEqual(result.name, filename)

    @unittest.skipIf(os.name == 'nt', "Windows does not support ? in filenames")
    def test_find_ebook_file_with_question_mark(self):
        """Test that filenames with question marks are found correctly."""
        from src.web_server import find_ebook_file
        import src.web_server

        src.web_server.EBOOK_DIR = Path(self.temp_dir)

        filename = "What If? - Science Questions.epub"
        test_file = Path(self.temp_dir) / filename
        test_file.touch()

        result = find_ebook_file(filename)
        self.assertIsNotNone(result)
        self.assertEqual(result.name, filename)

    def test_find_ebook_file_in_subdirectory(self):
        """Test that files in subdirectories are found."""
        from src.web_server import find_ebook_file
        import src.web_server

        src.web_server.EBOOK_DIR = Path(self.temp_dir)

        subdir = Path(self.temp_dir) / "Author Name"
        subdir.mkdir()
        filename = "Book [Series 01].epub"
        test_file = subdir / filename
        test_file.touch()

        result = find_ebook_file(filename)
        self.assertIsNotNone(result)
        self.assertEqual(result.name, filename)

if __name__ == '__main__':
    print("TEST Clean Flask Integration Testing with Dependency Injection")
    print("=" * 70)
    print("- No patches required")
    print("- Clean dependency injection")
    print("- Real HTTP requests via test_client()")
    print("- Mocked external services")
    print("- Easy to understand and maintain")
    print("=" * 70)

    unittest.main(verbosity=2)
