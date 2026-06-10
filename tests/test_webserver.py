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


def _http_response(status_code, payload=None, text="", headers=None):
    response = Mock()
    response.status_code = status_code
    response.text = text
    response.headers = headers or {}
    response.json.return_value = payload or {}
    return response


class MockContainer:
    """Mock container for testing - implements the same interface as real container."""

    def __init__(self):
        self.mock_sync_manager = Mock()
        self.mock_abs_client = Mock()
        self.mock_booklore_client = Mock()
        self.mock_storygraph_client = Mock()
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

    def storygraph_client(self):
        return self.mock_storygraph_client

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
        self.mock_storygraph_client = self.mock_container.mock_storygraph_client
        self.mock_database_service = self.mock_container.mock_database_service

        self.mock_database_service.get_all_books.return_value = []
        self.mock_database_service.get_all_states.return_value = []
        self.mock_database_service.get_all_hardcover_details.return_value = []
        self.mock_database_service.get_all_storygraph_details.return_value = []
        self.mock_database_service.get_all_pending_suggestions.return_value = []
        self.mock_database_service.get_all_reading_stats.return_value = {}
        self.mock_database_service.get_booklore_book.return_value = None
        self.mock_database_service.get_all_booklore_books.return_value = []
        self.mock_abs_client.get_all_audiobooks.return_value = []
        self.mock_abs_client.get_all_progress_raw.return_value = {}
        self.mock_booklore_client.is_configured.return_value = False
        self.mock_storygraph_client.is_configured.return_value = False
        self.mock_storyteller_client.is_configured.return_value = False

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

    def _set_dashboard_integrations(
        self,
        storyteller=False,
        booklore=False,
        bookloreaudio=False,
        hardcover=False,
        storygraph=False,
    ):
        clients_dict = {
            'ABS': Mock(is_configured=Mock(return_value=True)),
            'KoSync': Mock(is_configured=Mock(return_value=True)),
            'Storyteller': Mock(is_configured=Mock(return_value=storyteller)),
            'BookLore': Mock(is_configured=Mock(return_value=booklore)),
            'BookLoreAudio': Mock(is_configured=Mock(return_value=bookloreaudio)),
            'Hardcover': Mock(is_configured=Mock(return_value=hardcover)),
            'StoryGraph': Mock(is_configured=Mock(return_value=storygraph)),
        }
        self.mock_container.mock_sync_clients.items.return_value = clients_dict.items()

    def _capture_index_mapping(self):
        import src.web_server
        original_render = src.web_server.render_template
        mock_render = Mock(return_value="Mocked HTML Response")
        src.web_server.render_template = mock_render

        try:
            response = self.client.get('/')
            self.assertEqual(response.status_code, 200)
            return mock_render.call_args.kwargs['mappings'][0]
        finally:
            src.web_server.render_template = original_render

    def _render_index_template_source(self):
        import src.web_server
        template_source = (Path(__file__).parent.parent / 'templates' / 'index.html').read_text(encoding='utf-8')
        original_render = src.web_server.render_template

        def render_from_source(_template_name, **context):
            return src.web_server.render_template_string(template_source, **context)

        src.web_server.render_template = render_from_source
        try:
            response = self.client.get('/')
            self.assertEqual(response.status_code, 200)
            return response.get_data(as_text=True)
        finally:
            src.web_server.render_template = original_render

    def _read_template_source(self, template_name: str) -> str:
        return (Path(__file__).parent.parent / 'templates' / template_name).read_text(encoding='utf-8')

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
        self.mock_database_service.get_all_states.return_value = mock_states
        self.mock_database_service.get_hardcover_details.return_value = None
        self.mock_database_service.get_all_hardcover_details.return_value = []
        self.mock_database_service.get_all_storygraph_details.return_value = []
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
            self.mock_database_service.get_all_storygraph_details.assert_called_once()
            self.mock_database_service.get_all_booklore_books.assert_called_once()
            self.mock_abs_client.get_all_audiobooks.assert_not_called()
            self.mock_abs_client.get_all_progress_raw.assert_not_called()
            self.mock_storyteller_client.get_book_details.assert_not_called()
            self.mock_booklore_client.find_book_by_filename.assert_not_called()

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
        self.mock_database_service.get_all_states.return_value = []

        # Make HTTP request
        response = self.client.get('/api/status')

        # Verify response
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content_type, 'application/json')

        data = response.get_json()
        self.assertIn('mappings', data)
        self.assertEqual(len(data['mappings']), 1)
        self.assertEqual(data['mappings'][0]['abs_id'], 'api-test-book-123')
        self.assertEqual(data['mappings'][0]['unified_progress'], 0)
        self.assertEqual(data['mappings'][0]['last_sync'], 'Never')
        
        # Verify percentage scaling (should be 0 because states mock returned empty list)
        # But let's verify structure
        self.assertIn('states', data['mappings'][0])
        self.mock_database_service.get_all_states.assert_called_once()
        self.mock_database_service.get_states_for_book.assert_not_called()

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
        self.assertEqual(mapping['unified_progress'], 45.5)
        self.assertIn('last_sync', mapping)
        self.mock_database_service.get_states_for_book.assert_not_called()

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

    def test_match_endpoint_creates_abs_ebook_only_mapping_with_real_abs_id(self):
        """ABS ebook-only matches should preserve the real ABS ebook item ID."""
        import src.web_server

        original_get_kosync = src.web_server.get_kosync_id_for_ebook
        src.web_server.get_kosync_id_for_ebook = Mock(return_value='abs-ebook-kosync-id')

        try:
            self.mock_abs_client.get_all_audiobooks.return_value = []
            self.mock_abs_client.get_item_details.return_value = {
                'media': {
                    'metadata': {
                        'title': 'ABS Ebook Title'
                    }
                }
            }
            self.mock_database_service.get_book_by_kosync_id.return_value = None
            self.mock_database_service.get_book.return_value = None
            self.mock_booklore_client.is_configured.return_value = False

            response = self.client.post('/match', data={
                'ebook_filename': 'abs-ebook-123_abs.epub',
                'ebook_source': 'ABS',
                'ebook_source_id': 'abs-ebook-123'
            })

            self.assertEqual(response.status_code, 302)
            self.assertTrue(response.location.endswith('/'))

            self.mock_database_service.save_book.assert_called_once()
            saved_book = self.mock_database_service.save_book.call_args[0][0]

            self.assertEqual(saved_book.abs_id, 'abs-ebook-123')
            self.assertEqual(saved_book.abs_title, 'ABS Ebook Title')
            self.assertEqual(saved_book.sync_mode, 'ebook_only')
            self.assertEqual(saved_book.ebook_source, 'ABS')
            self.assertEqual(saved_book.ebook_source_id, 'abs-ebook-123')
            self.assertEqual(saved_book.abs_ebook_item_id, 'abs-ebook-123')
            self.assertEqual(saved_book.original_ebook_filename, 'abs-ebook-123_abs.epub')
            self.assertEqual(saved_book.kosync_doc_id, 'abs-ebook-kosync-id')
            self.mock_database_service.migrate_book_data.assert_not_called()
            self.mock_database_service.delete_book.assert_not_called()
        finally:
            src.web_server.get_kosync_id_for_ebook = original_get_kosync

    def test_match_endpoint_migrates_synthetic_abs_ebook_only_mapping_to_real_abs_id(self):
        """ABS ebook-only rematches should fold old synthetic rows into the real ABS ebook item ID."""
        import src.web_server
        from src.db.models import Book

        original_get_kosync = src.web_server.get_kosync_id_for_ebook
        src.web_server.get_kosync_id_for_ebook = Mock(return_value='abs-ebook-kosync-id')

        try:
            legacy_book = Book(
                abs_id='ebook-abs-ebook-kosy',
                abs_title='Legacy Synthetic Title',
                ebook_filename='legacy.epub',
                original_ebook_filename='legacy.epub',
                kosync_doc_id='abs-ebook-kosync-id',
                sync_mode='ebook_only',
                status='active'
            )
            self.mock_abs_client.get_all_audiobooks.return_value = []
            self.mock_abs_client.get_item_details.return_value = {
                'media': {
                    'metadata': {
                        'title': 'ABS Ebook Title'
                    }
                }
            }
            self.mock_database_service.get_book_by_kosync_id.return_value = legacy_book
            self.mock_database_service.get_book.return_value = None
            self.mock_booklore_client.is_configured.return_value = False

            response = self.client.post('/match', data={
                'ebook_filename': 'abs-ebook-123_abs.epub',
                'ebook_source': 'ABS',
                'ebook_source_id': 'abs-ebook-123'
            })

            self.assertEqual(response.status_code, 302)
            self.assertTrue(response.location.endswith('/'))

            self.mock_database_service.save_book.assert_called_once()
            saved_book = self.mock_database_service.save_book.call_args[0][0]

            self.assertEqual(saved_book.abs_id, 'abs-ebook-123')
            self.assertEqual(saved_book.abs_title, 'ABS Ebook Title')
            self.assertEqual(saved_book.sync_mode, 'ebook_only')
            self.assertEqual(saved_book.ebook_source, 'ABS')
            self.assertEqual(saved_book.ebook_source_id, 'abs-ebook-123')
            self.assertEqual(saved_book.abs_ebook_item_id, 'abs-ebook-123')
            self.mock_database_service.migrate_book_data.assert_called_once_with(
                'ebook-abs-ebook-kosy',
                'abs-ebook-123'
            )
            self.mock_database_service.delete_book.assert_called_once_with('ebook-abs-ebook-kosy')
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
        self.mock_database_service.get_all_booklore_books.return_value = [cached_book]
        self.mock_booklore_client.is_configured.return_value = False

        self._set_dashboard_integrations(storyteller=False)

        mapping = self._capture_index_mapping()
        self.assertEqual(mapping['abs_title'], 'Displayed Title')
        self.assertEqual(mapping['abs_subtitle'], 'Displayed Subtitle')
        self.assertEqual(mapping['abs_author'], 'Displayed Author')
        self.assertEqual(mapping['display_title'], 'Displayed Title')
        self.assertEqual(mapping['display_subtitle'], 'Displayed Subtitle')
        self.assertEqual(mapping['display_author'], 'Displayed Author')
        self.assertEqual(mapping['display_filename'], 'book-file.epub')

    def test_index_endpoint_storyteller_uses_cached_audio_title_for_display(self):
        from src.db.models import Book

        test_book = Book(
            abs_id='ebook-storyteller-1',
            abs_title='storyteller_uuid-book',
            ebook_filename='storyteller_uuid-book.epub',
            audio_title='Storyteller Title',
            storyteller_uuid='uuid-story-1',
            sync_mode='ebook_only',
            status='active'
        )

        self.mock_database_service.get_all_books.return_value = [test_book]
        self.mock_database_service.get_all_states.return_value = []
        self.mock_database_service.get_all_hardcover_details.return_value = []
        self.mock_database_service.get_all_pending_suggestions.return_value = []
        self.mock_booklore_client.is_configured.return_value = False
        self.mock_storyteller_client.is_configured.return_value = True

        self._set_dashboard_integrations(storyteller=True)

        mapping = self._capture_index_mapping()
        self.assertEqual(mapping['abs_title'], 'Storyteller Title')
        self.assertEqual(mapping['abs_subtitle'], '')
        self.assertEqual(mapping['abs_author'], '')
        self.assertEqual(mapping['display_title'], 'Storyteller Title')
        self.assertEqual(mapping['display_subtitle'], '')
        self.assertEqual(mapping['display_author'], '')
        self.assertEqual(mapping['display_filename'], 'storyteller_uuid-book.epub')
        self.mock_storyteller_client.get_book_details.assert_not_called()

    def test_index_endpoint_parses_filename_fallback_with_year(self):
        from src.db.models import Book

        test_book = Book(
            abs_id='ebook-filename-year-1',
            abs_title='Hearts Strange and Dreadful - Tim McGregor (2021)',
            ebook_filename='Hearts Strange and Dreadful - Tim McGregor (2021).epub',
            sync_mode='ebook_only',
            status='active'
        )

        self.mock_database_service.get_all_books.return_value = [test_book]
        self.mock_database_service.get_all_states.return_value = []
        self.mock_database_service.get_all_hardcover_details.return_value = []
        self.mock_database_service.get_all_pending_suggestions.return_value = []
        self.mock_booklore_client.is_configured.return_value = False
        self._set_dashboard_integrations(storyteller=False)

        mapping = self._capture_index_mapping()
        self.assertEqual(mapping['display_title'], 'Hearts Strange and Dreadful')
        self.assertEqual(mapping['display_subtitle'], '')
        self.assertEqual(mapping['display_author'], 'Tim McGregor')
        self.assertEqual(mapping['display_filename'], 'Hearts Strange and Dreadful - Tim McGregor (2021).epub')

    def test_index_endpoint_parses_filename_fallback_without_year(self):
        from src.db.models import Book

        test_book = Book(
            abs_id='ebook-filename-plain-1',
            abs_title='Delta-V - Daniel Suarez',
            ebook_filename='Delta-V - Daniel Suarez.epub',
            sync_mode='ebook_only',
            status='active'
        )

        self.mock_database_service.get_all_books.return_value = [test_book]
        self.mock_database_service.get_all_states.return_value = []
        self.mock_database_service.get_all_hardcover_details.return_value = []
        self.mock_database_service.get_all_pending_suggestions.return_value = []
        self.mock_booklore_client.is_configured.return_value = False
        self._set_dashboard_integrations(storyteller=False)

        mapping = self._capture_index_mapping()
        self.assertEqual(mapping['display_title'], 'Delta-V')
        self.assertEqual(mapping['display_subtitle'], '')
        self.assertEqual(mapping['display_author'], 'Daniel Suarez')
        self.assertEqual(mapping['display_filename'], 'Delta-V - Daniel Suarez.epub')

    def test_index_endpoint_uses_plain_stem_when_filename_has_no_author_pattern(self):
        from src.db.models import Book

        test_book = Book(
            abs_id='ebook-plain-stem-1',
            abs_title='plain-stem-book',
            ebook_filename='plain-stem-book.epub',
            sync_mode='ebook_only',
            status='active'
        )

        self.mock_database_service.get_all_books.return_value = [test_book]
        self.mock_database_service.get_all_states.return_value = []
        self.mock_database_service.get_all_hardcover_details.return_value = []
        self.mock_database_service.get_all_pending_suggestions.return_value = []
        self.mock_booklore_client.is_configured.return_value = False
        self._set_dashboard_integrations(storyteller=False)

        mapping = self._capture_index_mapping()
        self.assertEqual(mapping['display_title'], 'plain-stem-book')
        self.assertEqual(mapping['display_subtitle'], '')
        self.assertEqual(mapping['display_author'], '')
        self.assertEqual(mapping['display_filename'], 'plain-stem-book.epub')

    def test_index_endpoint_keeps_trustworthy_audiobook_metadata(self):
        from src.db.models import Book, BookloreBook

        test_book = Book(
            abs_id='audio-backed-1',
            abs_title='ABS Clean Title',
            ebook_filename='Audio Title - Wrong Author.epub',
            sync_mode='audiobook',
            status='active'
        )
        cached_book = BookloreBook(
            filename='Audio Title - Wrong Author.epub',
            title='Cached Ebook Title',
            authors='Cached Ebook Author',
            raw_metadata=json.dumps({
                'title': 'Cached Ebook Title',
                'subtitle': 'Cached Ebook Subtitle',
                'authors': 'Cached Ebook Author'
            })
        )

        self.mock_database_service.get_all_books.return_value = [test_book]
        self.mock_database_service.get_all_states.return_value = []
        self.mock_database_service.get_all_hardcover_details.return_value = []
        self.mock_database_service.get_all_pending_suggestions.return_value = []
        self.mock_database_service.get_all_booklore_books.return_value = [cached_book]
        self.mock_booklore_client.is_configured.return_value = False
        self._set_dashboard_integrations(storyteller=False)

        mapping = self._capture_index_mapping()
        self.assertEqual(mapping['display_title'], 'ABS Clean Title')
        self.assertEqual(mapping['display_subtitle'], 'Cached Ebook Subtitle')
        self.assertEqual(mapping['display_author'], 'Cached Ebook Author')
        self.assertEqual(mapping['display_filename'], 'Audio Title - Wrong Author.epub')

    def test_index_endpoint_sync_warning_ignores_disabled_storyteller_state(self):
        from src.db.models import Book, State

        test_book = Book(
            abs_id='sync-warning-1',
            abs_title='Trad Wife',
            ebook_filename='storyteller_uuid-book.epub',
            storyteller_uuid='uuid-story-1',
            sync_mode='audiobook',
            status='active',
            duration=40269
        )
        mock_states = [
            State(abs_id='sync-warning-1', client_name='abs', percentage=0.8618398680719048, timestamp=34706, last_updated=1642291400),
            State(abs_id='sync-warning-1', client_name='kosync', percentage=0.814176695624452, xpath='/body/DocFragment[37]/body/section/p[62]/text().0', last_updated=1642291400),
            State(abs_id='sync-warning-1', client_name='booklore', percentage=0.814176695624452, cfi='epubcfi(/6/74!/4/2/130/2:0)', last_updated=1642291400),
            State(abs_id='sync-warning-1', client_name='storyteller', percentage=0.322427671953455, cfi='epubcfi(/6/34!/4/2/174/2:0)', last_updated=1642240000),
        ]

        self.mock_database_service.get_all_books.return_value = [test_book]
        self.mock_database_service.get_all_states.return_value = mock_states
        self.mock_database_service.get_all_hardcover_details.return_value = []
        self.mock_database_service.get_all_pending_suggestions.return_value = []
        self.mock_booklore_client.is_configured.return_value = True
        from src.db.models import BookloreBook
        self.mock_database_service.get_all_booklore_books.return_value = [
            BookloreBook(filename='storyteller_uuid-book.epub', raw_metadata=json.dumps({'id': 10440}))
        ]
        self._set_dashboard_integrations(storyteller=False, booklore=True)

        mapping = self._capture_index_mapping()
        self.assertIn('storyteller', mapping['states'])
        self.assertEqual(mapping['sync_warning_pct'], 4.8)
        self.assertFalse(mapping['is_out_of_sync'])
        self.mock_booklore_client.find_book_by_filename.assert_not_called()

        html = self._render_index_template_source()
        self.assertNotIn('Out of sync by 54.0%', html)
        self.assertNotIn('class="book-card out-of-sync"', html)

    def test_index_template_keeps_filename_searchable_but_not_as_author(self):
        from src.db.models import Book, BookloreBook

        test_book = Book(
            abs_id='ebook-template-1',
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
        self.mock_database_service.get_all_booklore_books.return_value = [cached_book]
        self.mock_booklore_client.is_configured.return_value = False
        self._set_dashboard_integrations(storyteller=False)

        html = self._render_index_template_source()
        self.assertIn('data-filename="book-file.epub"', html)
        self.assertIn('data-author="Displayed Author"', html)
        self.assertIn('Displayed Title', html)
        self.assertIn('Displayed Subtitle', html)
        self.assertIn('<div class="book-author">Displayed Author</div>', html)
        self.assertNotIn('<div class="book-author">book-file.epub</div>', html)
        self.assertIn('loading="lazy"', html)
        self.assertIn('decoding="async"', html)

    def test_index_endpoint_uses_cached_storygraph_and_goodreads_ratings(self):
        from src.db.models import Book, BookloreBook, StorygraphDetails

        test_book = Book(
            abs_id='ratings-1',
            abs_title='Ratings Book',
            ebook_filename='ratings.epub',
            sync_mode='ebook_only',
            status='active'
        )
        cached_book = BookloreBook(
            filename='ratings.epub',
            title='Ratings Book',
            authors='Rating Author',
            raw_metadata=json.dumps({
                'metadata': {
                    'goodreadsRating': 4.12,
                    'goodreadsReviewCount': 3456,
                }
            })
        )
        storygraph_details = StorygraphDetails(
            abs_id='ratings-1',
            storygraph_book_id='sg-ratings',
            storygraph_url='https://app.thestorygraph.com/books/sg-ratings',
            storygraph_rating=3.78,
            storygraph_review_count=9305,
        )

        self.mock_database_service.get_all_books.return_value = [test_book]
        self.mock_database_service.get_all_states.return_value = []
        self.mock_database_service.get_all_hardcover_details.return_value = []
        self.mock_database_service.get_all_storygraph_details.return_value = [storygraph_details]
        self.mock_database_service.get_all_pending_suggestions.return_value = []
        self.mock_database_service.get_all_booklore_books.return_value = [cached_book]
        self.mock_booklore_client.is_configured.return_value = False
        self._set_dashboard_integrations(storygraph=True)

        mapping = self._capture_index_mapping()

        self.assertEqual(mapping['storygraph_rating'], 3.78)
        self.assertEqual(mapping['storygraph_review_count'], 9305)
        self.assertEqual(mapping['goodreads_rating'], 4.12)
        self.assertEqual(mapping['goodreads_review_count'], 3456)
        self.mock_storygraph_client.get_book_rating.assert_not_called()

    def test_index_template_renders_rating_badges_under_cover(self):
        from src.db.models import Book, BookloreBook, StorygraphDetails

        self.mock_database_service.get_all_books.return_value = [
            Book(abs_id='ratings-template-1', abs_title='Ratings Template', ebook_filename='ratings-template.epub', status='active')
        ]
        self.mock_database_service.get_all_states.return_value = []
        self.mock_database_service.get_all_hardcover_details.return_value = []
        self.mock_database_service.get_all_storygraph_details.return_value = [
            StorygraphDetails(
                abs_id='ratings-template-1',
                storygraph_book_id='sg-ratings-template',
                storygraph_rating=3.78,
                storygraph_review_count=9305,
            )
        ]
        self.mock_database_service.get_all_booklore_books.return_value = [
            BookloreBook(filename='ratings-template.epub', raw_metadata=json.dumps({
                'metadata': {'goodreadsRating': 4.12, 'goodreadsReviewCount': 3456}
            }))
        ]
        self.mock_database_service.get_all_pending_suggestions.return_value = []
        self.mock_booklore_client.is_configured.return_value = False
        self._set_dashboard_integrations(storygraph=True)

        html = self._render_index_template_source()

        self.assertIn('class="book-media-column"', html)
        self.assertIn('class="rating-badge rating-badge-sg"', html)
        self.assertIn('class="rating-badge rating-badge-gr"', html)
        self.assertIn('<span class="rating-source">SG</span>', html)
        self.assertIn('<span class="rating-value">3.8</span>', html)
        self.assertIn('<span class="rating-source">GR</span>', html)
        self.assertIn('<span class="rating-value">4.1</span>', html)
        self.assertIn('StoryGraph: 3.78 based on 9,305 reviews', html)
        self.assertIn('Goodreads: 4.12 based on 3,456 reviews', html)
        # data-rating uses the avg of both sources (3.78 + 4.12) / 2 = 3.95
        self.assertIn('data-rating="3.95"', html)
        # Rating option is in the sort dropdown
        self.assertIn('<option value="rating">Rating</option>', html)
        # Series-grouping checkbox is present, default-checked, and the JS toggle handler is wired.
        self.assertIn('id="series-grouping-toggle"', html)
        self.assertIn('abs_kosync_series_grouping', html)
        self.assertIn('series-grouping-off', html)
        self.mock_storygraph_client.get_book_rating.assert_not_called()

    def test_index_template_renders_three_progress_sections_and_not_started_toggle(self):
        from src.db.models import Book, State

        in_progress_book = Book(
            abs_id='in-progress-1',
            abs_title='In Progress Book',
            ebook_filename='in-progress.epub',
            status='active',
            duration=3600
        )
        not_started_book = Book(
            abs_id='not-started-1',
            abs_title='Not Started Book',
            ebook_filename='not-started.epub',
            sync_mode='ebook_only',
            status='active'
        )
        finished_book = Book(
            abs_id='finished-1',
            abs_title='Finished Book',
            ebook_filename='finished.epub',
            status='active',
            duration=3600
        )

        self.mock_database_service.get_all_books.return_value = [
            in_progress_book,
            not_started_book,
            finished_book,
        ]
        self.mock_database_service.get_all_states.return_value = [
            State(abs_id='in-progress-1', client_name='kosync', percentage=0.45, last_updated=1000),
            State(abs_id='finished-1', client_name='abs', percentage=1.0, timestamp=3600, last_updated=2000),
        ]
        self._set_dashboard_integrations(storyteller=False)

        html = self._render_index_template_source()

        self.assertIn('In Progress', html)
        self.assertIn('Not Started', html)
        self.assertIn('Finished', html)
        self.assertNotIn('Currently Reading', html)
        self.assertNotIn('<h2>All Books</h2>', html)
        self.assertLess(html.index('In Progress'), html.index('Not Started'))
        self.assertLess(html.index('Not Started'), html.index('Finished'))

        self.assertIn('id="in-progress-section"', html)
        self.assertIn('id="not-started-section"', html)
        self.assertIn('id="finished-section"', html)
        self.assertIn('id="not-started-header"', html)
        self.assertIn('id="not-started-grid"', html)
        self.assertIn('id="not-started-chevron"', html)
        self.assertIn("localStorage.setItem('not_started_expanded', isShowing);", html)
        self.assertIn("const shouldExpand = localStorage.getItem('not_started_expanded') === 'true';", html)
        self.assertIn("window.toggleNotStarted = function ()", html)

        in_progress_chunk = html.split('id="in-progress-section"', 1)[1].split('id="not-started-section"', 1)[0]
        not_started_chunk = html.split('id="not-started-section"', 1)[1].split('id="finished-section"', 1)[0]
        finished_chunk = html.split('id="finished-section"', 1)[1]

        self.assertIn('data-abs-id="in-progress-1"', in_progress_chunk)
        self.assertNotIn('data-abs-id="not-started-1"', in_progress_chunk)
        self.assertNotIn('data-abs-id="finished-1"', in_progress_chunk)

        self.assertIn('data-abs-id="not-started-1"', not_started_chunk)
        self.assertIn('data-filename="not-started.epub"', not_started_chunk)
        self.assertNotIn('data-abs-id="in-progress-1"', not_started_chunk)
        self.assertNotIn('data-abs-id="finished-1"', not_started_chunk)

        self.assertIn('data-abs-id="finished-1"', finished_chunk)
        self.assertNotIn('data-abs-id="in-progress-1"', finished_chunk)
        self.assertNotIn('data-abs-id="not-started-1"', finished_chunk)

    def test_index_template_hides_empty_progress_sections(self):
        from src.db.models import Book

        not_started_book = Book(
            abs_id='only-not-started-1',
            abs_title='Only Not Started',
            ebook_filename='only-not-started.epub',
            status='active'
        )

        self.mock_database_service.get_all_books.return_value = [not_started_book]
        self._set_dashboard_integrations(storyteller=False)

        html = self._render_index_template_source()

        self.assertNotIn('id="in-progress-section"', html)
        self.assertIn('id="not-started-section"', html)
        self.assertNotIn('id="finished-section"', html)
        self.assertNotIn('<h2>In Progress</h2>', html)
        self.assertIn('<h2>Not Started</h2>', html)
        self.assertNotIn('<h2>Finished</h2>', html)
        self.assertIn('class="book-grid collapsible-book-grid" id="not-started-grid"', html)
        self.assertNotIn('No books syncing yet', html)

    def test_match_template_has_submit_feedback_hooks(self):
        html = self._read_template_source('match.html')

        self.assertIn('id="submitFeedback"', html)
        self.assertIn('data-working-label="Creating mapping..."', html)
        self.assertIn('data-modal-label="Opening forge options..."', html)
        self.assertIn('previewMatchSubmit(', html)
        self.assertIn('mappingForm.requestSubmit(forgeBtn);', html)

    def test_batch_match_template_has_submit_feedback_hooks(self):
        html = self._read_template_source('batch_match.html')

        self.assertIn('id="selectionFeedback"', html)
        self.assertIn('id="queueFeedback"', html)
        self.assertIn('data-working-label="Adding to queue..."', html)
        self.assertIn('data-working-label="Processing queue..."', html)
        self.assertIn('data-working-label="Forging + matching..."', html)
        self.assertIn('startBatchSubmitState(submitter)', html)

    def test_suggestions_template_has_submit_feedback_hooks(self):
        html = self._read_template_source('suggestions.html')

        self.assertIn('id="selectionFeedback"', html)
        self.assertIn('id="queueFeedback"', html)
        self.assertIn("addToQueueBtn.dataset.primaryAction = 'selection-actions';", html)
        self.assertIn("processQueueBtn.dataset.primaryAction = 'queue-actions';", html)
        self.assertIn('startSuggestionSubmitState(submitter)', html)

    def test_forge_template_has_submit_feedback_hooks(self):
        html = self._read_template_source('forge.html')

        self.assertIn('previewForgeState(', html)
        self.assertIn('Opening forge options...', html)
        self.assertIn('forgeRequestInFlight = true;', html)
        self.assertIn("btn.textContent = 'Forging edition...';", html)

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

    def test_get_abs_libraries_returns_available_libraries(self):
        self.mock_abs_client.is_configured.return_value = True
        self.mock_abs_client.get_libraries.return_value = [
            {'id': 'lib-audio', 'name': 'Audiobooks', 'mediaType': 'book'},
            {'id': 'lib-podcasts', 'name': 'Podcasts', 'mediaType': 'podcast'},
        ]

        response = self.client.get('/api/abs/libraries')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), [
            {'id': 'lib-audio', 'name': 'Audiobooks', 'mediaType': 'book'},
            {'id': 'lib-podcasts', 'name': 'Podcasts', 'mediaType': 'podcast'},
        ])

    def test_get_abs_libraries_requires_configured_abs(self):
        self.mock_abs_client.is_configured.return_value = False

        response = self.client.get('/api/abs/libraries')

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json(), {'error': 'Audiobookshelf not configured'})

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

    @patch('src.web_server.requests.get')
    def test_test_connection_builtin_kosync_accepts_typed_unsaved_credentials(self, mock_get):
        def fake_get(url, headers=None, timeout=None):
            self.assertEqual(url, 'http://127.0.0.1:5757/healthcheck')
            self.assertEqual(headers['x-auth-user'], 'typed-reader')
            self.assertEqual(headers['x-auth-key'], hash_kosync_key('typed-pass'))
            self.assertEqual(headers['accept'], KOSYNC_ACCEPT)
            self.assertEqual(timeout, 5)
            return _http_response(200)

        mock_get.side_effect = fake_get

        with patch.dict(os.environ, {
            'KOSYNC_PORT': '5757',
            'KOSYNC_USER': 'saved-reader',
            'KOSYNC_KEY': 'saved-pass',
        }, clear=False):
            response = self.client.post(
                '/api/test-connection/kosync',
                json={
                    'KOSYNC_ENABLED': True,
                    'KOSYNC_SERVER': '127.0.0.1:5757',
                    'KOSYNC_USER': 'typed-reader',
                    'KOSYNC_KEY': 'typed-pass',
                },
            )

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data['ok'])
        self.assertIn('will take effect after you save settings', data['message'])
        mock_get.assert_called_once()

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

    @patch('src.web_server.requests.get')
    def test_test_connection_storygraph_valid_cookies(self, mock_get):
        def fake_get(url, headers=None, timeout=None, allow_redirects=None):
            self.assertEqual(url, 'https://app.thestorygraph.com/users/sign_in')
            self.assertEqual(timeout, 10)
            self.assertFalse(allow_redirects)
            self.assertIn('_storygraph_session=session-cookie', headers['Cookie'])
            self.assertIn('remember_user_token=remember-token', headers['Cookie'])
            self.assertNotIn('_story_graph_session', headers['Cookie'])
            return _http_response(302, headers={'Location': '/books/book-1'})

        mock_get.side_effect = fake_get

        response = self.client.post(
            '/api/test-connection/storygraph',
            json={
                'STORYGRAPH_ENABLED': True,
                'STORYGRAPH_SESSION_COOKIE': 'session-cookie',
                'STORYGRAPH_REMEMBER_USER_TOKEN': 'remember-token',
            },
        )

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data['ok'])
        self.assertEqual(data['message'], 'StoryGraph session accepted')

    @patch('src.web_server.requests.get')
    def test_test_connection_storygraph_sign_in_redirect_is_invalid(self, mock_get):
        mock_get.return_value = _http_response(302, headers={'Location': '/users/sign_in'})

        response = self.client.post(
            '/api/test-connection/storygraph',
            json={
                'STORYGRAPH_ENABLED': True,
                'STORYGRAPH_SESSION_COOKIE': 'session-cookie',
                'STORYGRAPH_REMEMBER_USER_TOKEN': 'remember-token',
            },
        )

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertFalse(data['ok'])
        self.assertEqual(data['message'], 'Invalid StoryGraph session cookies')

    @patch('src.web_server.requests.get')
    def test_test_connection_storygraph_disabled(self, mock_get):
        response = self.client.post(
            '/api/test-connection/storygraph',
            json={
                'STORYGRAPH_ENABLED': False,
                'STORYGRAPH_SESSION_COOKIE': 'session-cookie',
                'STORYGRAPH_REMEMBER_USER_TOKEN': 'remember-token',
            },
        )

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertFalse(data['ok'])
        self.assertEqual(data['message'], 'StoryGraph is disabled')
        mock_get.assert_not_called()

    def test_storygraph_card_renders_on_dashboard(self):
        from src.db.models import Book, State, StorygraphDetails

        self._set_dashboard_integrations(storygraph=True)
        self.mock_database_service.get_all_books.return_value = [
            Book(abs_id='abs-storygraph-1', abs_title='Story Book', ebook_filename='story.epub')
        ]
        self.mock_database_service.get_all_states.return_value = [
            State(abs_id='abs-storygraph-1', client_name='storygraph', percentage=0.37, last_updated=1)
        ]
        self.mock_database_service.get_all_storygraph_details.return_value = [
            StorygraphDetails(
                abs_id='abs-storygraph-1',
                storygraph_book_id='sg-1',
                storygraph_url='https://app.thestorygraph.com/books/sg-1',
                matched_by='manual',
            )
        ]

        html = self._render_index_template_source()

        self.assertIn('StoryGraph', html)
        self.assertIn('linkStorygraph(event)', html)
        self.assertIn('https://app.thestorygraph.com/books/sg-1', html)

    def test_api_storygraph_resolve_uses_abs_metadata(self):
        from src.db.models import Book

        self.mock_storygraph_client.is_configured.return_value = True
        self.mock_database_service.get_storygraph_details.return_value = None
        self.mock_database_service.get_book.return_value = Book(
            abs_id='abs-storygraph-1',
            abs_title='Bridge Book',
            ebook_filename='bridge.epub',
        )
        self.mock_abs_client.get_item_details.return_value = {
            'media': {'metadata': {'title': 'Bridge Book', 'authorName': 'Bridge Author', 'isbn': '1234567890'}}
        }
        self.mock_storygraph_client.resolve_book.return_value = {
            'book_id': 'sg-1',
            'title': 'Bridge Book',
            'author': 'Bridge Author',
            'url': 'https://app.thestorygraph.com/books/sg-1',
        }

        response = self.client.get('/api/storygraph/resolve?abs_id=abs-storygraph-1')

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data['found'])
        self.assertEqual(data['book_id'], 'sg-1')
        self.assertEqual(data['url'], 'https://app.thestorygraph.com/books/sg-1')
        self.mock_storygraph_client.resolve_book.assert_called_once_with(
            title='Bridge Book',
            author='Bridge Author',
            isbn='1234567890',
        )

    def test_link_storygraph_saves_details_and_sets_status(self):
        from src.db.models import Book

        self.mock_storygraph_client.is_configured.return_value = True
        self.mock_database_service.get_book.return_value = Book(
            abs_id='abs-storygraph-1',
            abs_title='Bridge Book',
            ebook_filename='bridge.epub',
        )
        self.mock_abs_client.get_item_details.return_value = {
            'media': {'metadata': {'isbn': '1234567890', 'asin': 'B000123'}}
        }

        response = self.client.post(
            '/link-storygraph/abs-storygraph-1',
            json={
                'book_id': 'sg-1',
                'title': 'Bridge Book',
                'url': 'https://app.thestorygraph.com/books/sg-1',
            },
        )

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data['success'])

        saved_details = self.mock_database_service.save_storygraph_details.call_args.args[0]
        self.assertEqual(saved_details.abs_id, 'abs-storygraph-1')
        self.assertEqual(saved_details.storygraph_book_id, 'sg-1')
        self.assertEqual(saved_details.storygraph_url, 'https://app.thestorygraph.com/books/sg-1')
        self.assertEqual(saved_details.isbn, '1234567890')
        self.assertEqual(saved_details.asin, 'B000123')
        self.assertEqual(saved_details.matched_by, 'manual')
        self.mock_storygraph_client.update_status.assert_called_once_with('sg-1', 1)

    def test_link_storygraph_saves_selected_edition_and_pages(self):
        from src.db.models import Book

        self.mock_storygraph_client.is_configured.return_value = True
        self.mock_database_service.get_book.return_value = Book(
            abs_id='abs-storygraph-2',
            abs_title='Story Book',
            ebook_filename='story.epub',
        )
        self.mock_abs_client.get_item_details.return_value = {
            'media': {'metadata': {'isbn': '1234567890', 'asin': 'B000123'}}
        }

        response = self.client.post(
            '/link-storygraph/abs-storygraph-2',
            json={
                'book_id': 'sg-2',
                'edition_id': 'ed-2',
                'pages': 420,
                'title': 'Story Book',
                'url': 'https://app.thestorygraph.com/books/sg-2',
            },
        )

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data['success'])

        saved_details = self.mock_database_service.save_storygraph_details.call_args.args[0]
        self.assertEqual(saved_details.abs_id, 'abs-storygraph-2')
        self.assertEqual(saved_details.storygraph_book_id, 'sg-2')
        self.assertEqual(saved_details.storygraph_edition_id, 'ed-2')
        self.assertEqual(saved_details.storygraph_pages, 420)
        self.assertEqual(saved_details.storygraph_url, 'https://app.thestorygraph.com/books/sg-2')
        self.assertEqual(saved_details.isbn, '1234567890')
        self.assertEqual(saved_details.asin, 'B000123')
        self.assertEqual(saved_details.matched_by, 'manual')
        self.mock_storygraph_client.update_status.assert_called_once_with('ed-2', 1)

    def test_link_storygraph_saves_audio_edition_as_pages_sentinel(self):
        from src.db.models import Book

        self.mock_storygraph_client.is_configured.return_value = True
        self.mock_database_service.get_book.return_value = Book(
            abs_id='abs-storygraph-3',
            abs_title='Story Audio Book',
            ebook_filename='story.epub',
        )
        self.mock_abs_client.get_item_details.return_value = {
            'media': {'metadata': {'isbn': '1234567890', 'asin': 'B000123'}}
        }

        response = self.client.post(
            '/link-storygraph/abs-storygraph-3',
            json={
                'book_id': 'sg-3',
                'edition_id': 'ed-3',
                'pages': 0,
                'audio_seconds': 12345,
                'title': 'Story Audio Book',
                'url': 'https://app.thestorygraph.com/books/sg-3',
            },
        )

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data['success'])

        saved_details = self.mock_database_service.save_storygraph_details.call_args.args[0]
        self.assertEqual(saved_details.storygraph_edition_id, 'ed-3')
        self.assertEqual(saved_details.storygraph_pages, -1)
        self.assertEqual(saved_details.storygraph_url, 'https://app.thestorygraph.com/books/sg-3')
        self.assertEqual(saved_details.matched_by, 'manual')
        self.mock_storygraph_client.update_status.assert_called_once_with('ed-3', 1)

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

    @patch('src.web_server._build_listening_stats_payload')
    @patch('src.web_server._write_cached_stats')
    @patch('src.web_server._read_cached_stats', return_value=None)
    def test_api_stats_combines_unlinked_koreader_books(self, _mock_read_cached, mock_write_cached, mock_listening_stats):
        today = '2026-04-07'
        mock_listening_stats.return_value = {
            'available': True,
            'stats': {
                'activeDays': 1,
                'totalSeconds': 900,
                'currentStreakDays': 1,
            },
            'daily': [{'date': today, 'seconds': 900}],
            'heatmap': [{'date': today, 'seconds': 900}],
            'recentSessions': [{
                'id': 'listening-1',
                'activityType': 'listening',
                'absId': 'abs-linked',
                'title': 'Linked Bridge Book',
                'durationSeconds': 900,
                'startedAt': 1712487600,
                'endedAt': 1712488500,
            }],
            'activityDates': [today],
            'trackedBookIds': ['abs-linked'],
        }
        self.mock_database_service.get_koreader_dashboard_summary.return_value = {
            'booksTracked': 2,
            'linkedBooksTracked': 1,
            'unlinkedBooksTracked': 1,
            'daysRead': 1,
            'totalSeconds': 600,
            'pagesRead': 4,
            'trackedBookIds': ['abs-linked'],
            'trackedBookKeys': ['abs:abs-linked', 'koreader:md5-unlinked'],
            'weekTotalSeconds': 600,
            'dailyAverageSeconds': 85,
            'bestDay': {'date': today, 'seconds': 600, 'pages': 4},
            'currentStreakDays': 1,
        }
        self.mock_database_service.get_koreader_daily_totals.return_value = [{'date': today, 'seconds': 600, 'pages': 4}]
        self.mock_database_service.get_koreader_heatmap.return_value = [{'date': today, 'seconds': 600, 'pages': 4}]
        self.mock_database_service.get_koreader_recent_sessions.return_value = [
            {
                'id': 'reading-linked',
                'activityType': 'reading',
                'bookKey': 'abs:abs-linked',
                'absId': 'abs-linked',
                'isLinked': True,
                'title': 'Linked Bridge Book',
                'author': 'Linked Author',
                'durationSeconds': 300,
                'pagesRead': 2,
                'startedAt': 1712484000,
                'endedAt': 1712484300,
                'deviceKey': 'device-1',
            },
            {
                'id': 'reading-unlinked',
                'activityType': 'reading',
                'bookKey': 'koreader:md5-unlinked',
                'absId': None,
                'isLinked': False,
                'title': 'Loose KOReader Book',
                'author': 'Unlinked Author',
                'durationSeconds': 300,
                'pagesRead': 2,
                'startedAt': 1712485000,
                'endedAt': 1712485300,
                'deviceKey': 'device-1',
            },
        ]
        self.mock_database_service.get_koreader_activity_dates.return_value = [today]
        self.mock_database_service.get_koreader_hour_histogram.return_value = [
            {'hour': h, 'seconds': 0, 'pages': 0} for h in range(24)
        ]
        self.mock_database_service.get_koreader_book_list.return_value = []
        self.mock_database_service.get_koreader_yearly_recap.return_value = {
            'year': 2026, 'months': [{'month': m, 'seconds': 0, 'pages': 0, 'finished': 0} for m in range(1, 13)],
            'totalSeconds': 0, 'totalPages': 0, 'booksFinished': 0, 'finishedBooks': [], 'availableYears': [],
        }

        response = self.client.get('/api/stats')

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data['reading']['available'])
        self.assertEqual(data['reading']['stats']['booksTracked'], 2)
        self.assertEqual(data['reading']['stats']['linkedBooksTracked'], 1)
        self.assertEqual(data['reading']['stats']['unlinkedBooksTracked'], 1)
        self.assertEqual(data['reading']['trackedBookKeys'], ['abs:abs-linked', 'koreader:md5-unlinked'])
        self.assertEqual(data['combined']['stats']['booksWithActivity'], 2)
        self.assertTrue(any(session['isLinked'] is False for session in data['reading']['recentSessions']))
        mock_write_cached.assert_called_once()

    def test_api_stats_reading_day_supports_unlinked_books(self):
        payload = {
            'date': '2026-04-07',
            'totalSeconds': 600,
            'totalPages': 4,
            'totalBooks': 2,
            'books': [
                {
                    'bookKey': 'abs:abs-linked',
                    'absId': 'abs-linked',
                    'isLinked': True,
                    'title': 'Linked Bridge Book',
                    'author': 'Linked Author',
                    'totalSeconds': 300,
                    'pagesRead': 2,
                    'sessionCount': 1,
                    'firstStartedAt': 1712484000,
                    'lastEndedAt': 1712484300,
                },
                {
                    'bookKey': 'koreader:md5-unlinked',
                    'absId': None,
                    'isLinked': False,
                    'title': 'Loose KOReader Book',
                    'author': 'Unlinked Author',
                    'totalSeconds': 300,
                    'pagesRead': 2,
                    'sessionCount': 1,
                    'firstStartedAt': 1712485000,
                    'lastEndedAt': 1712485300,
                },
            ],
        }
        self.mock_database_service.get_koreader_books_for_date.return_value = payload

        response = self.client.get('/api/stats/reading-day?date=2026-04-07')

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data['totalBooks'], 2)
        self.assertTrue(any(book['isLinked'] is False and book['absId'] is None for book in data['books']))


class TestStorytellerNoCacheFlag(CleanFlaskIntegrationTest):
    """Cover STORYTELLER_NO_EPUB_CACHE handling in _download_storyteller_artifact."""

    def setUp(self):
        super().setUp()
        self.epub_cache_dir = Path(self.temp_dir) / "epub_cache"
        self.epub_cache_dir.mkdir(parents=True, exist_ok=True)
        self.books_dir = Path(self.temp_dir) / "books"
        self.books_dir.mkdir(parents=True, exist_ok=True)
        self.mock_container.epub_cache_dir = lambda: self.epub_cache_dir

    def tearDown(self):
        os.environ.pop("STORYTELLER_NO_EPUB_CACHE", None)
        super().tearDown()

    def test_uses_original_when_flag_enabled_and_original_resolves(self):
        from src.web_server import _download_storyteller_artifact

        original_path = self.books_dir / "original.epub"
        original_path.write_bytes(b"epub bytes")
        self.mock_container.mock_ebook_parser.resolve_book_path.return_value = original_path
        os.environ["STORYTELLER_NO_EPUB_CACHE"] = "true"

        filename, path = _download_storyteller_artifact(
            "uuid-abc",
            "Some Title",
            original_ebook_filename="original.epub",
        )

        self.assertEqual(filename, "original.epub")
        self.assertEqual(Path(path), original_path)
        self.mock_storyteller_client.download_book.assert_not_called()

    def test_falls_back_to_download_when_original_missing(self):
        from src.web_server import _download_storyteller_artifact

        self.mock_container.mock_ebook_parser.resolve_book_path.side_effect = FileNotFoundError()
        self.mock_storyteller_client.download_book.return_value = True
        os.environ["STORYTELLER_NO_EPUB_CACHE"] = "true"

        filename, path = _download_storyteller_artifact(
            "uuid-abc",
            "Some Title",
            original_ebook_filename="original.epub",
        )

        self.assertEqual(filename, "storyteller_uuid-abc.epub")
        self.assertEqual(Path(path), self.epub_cache_dir / "storyteller_uuid-abc.epub")
        self.mock_storyteller_client.download_book.assert_called_once()

    def test_flag_ignored_when_original_filename_not_provided(self):
        from src.web_server import _download_storyteller_artifact

        self.mock_storyteller_client.download_book.return_value = True
        os.environ["STORYTELLER_NO_EPUB_CACHE"] = "true"

        filename, _path = _download_storyteller_artifact("uuid-abc", "Some Title")

        self.assertEqual(filename, "storyteller_uuid-abc.epub")
        self.mock_storyteller_client.download_book.assert_called_once()
        self.mock_container.mock_ebook_parser.resolve_book_path.assert_not_called()

    def test_flag_disabled_still_downloads(self):
        from src.web_server import _download_storyteller_artifact

        self.mock_storyteller_client.download_book.return_value = True
        os.environ["STORYTELLER_NO_EPUB_CACHE"] = "false"

        filename, _path = _download_storyteller_artifact(
            "uuid-abc",
            "Some Title",
            original_ebook_filename="original.epub",
        )

        self.assertEqual(filename, "storyteller_uuid-abc.epub")
        self.mock_storyteller_client.download_book.assert_called_once()
        self.mock_container.mock_ebook_parser.resolve_book_path.assert_not_called()

    def test_api_storyteller_link_no_cache_stores_original_filename(self):
        from src.db.models import Book

        original_path = self.books_dir / "original.epub"
        original_path.write_bytes(b"epub bytes")
        self.mock_container.mock_ebook_parser.resolve_book_path.return_value = original_path
        os.environ["STORYTELLER_NO_EPUB_CACHE"] = "true"

        test_book = Book(
            abs_id="story-link-nocache",
            abs_title="Story Link",
            ebook_filename="original.epub",
            storyteller_uuid=None,
            transcript_source=None,
            transcript_file=None,
            status="active",
        )
        self.mock_database_service.get_book.return_value = test_book
        self.mock_abs_client.get_item_details.return_value = {
            "media": {"chapters": [{"start": 0.0, "end": 10.0}]}
        }

        with patch("src.web_server.ingest_storyteller_transcripts", return_value=None):
            response = self.client.post(
                "/api/storyteller/link/story-link-nocache",
                json={"uuid": "uuid-no-cache"},
            )

        self.assertEqual(response.status_code, 200)
        self.mock_storyteller_client.download_book.assert_not_called()
        self.mock_database_service.save_book.assert_called_once()
        saved_book = self.mock_database_service.save_book.call_args[0][0]
        self.assertEqual(saved_book.ebook_filename, "original.epub")
        self.assertEqual(saved_book.storyteller_uuid, "uuid-no-cache")


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


class TestAudiobookSearchVariants(unittest.TestCase):
    """Filename-style suggestion titles relax to the bare title so ABS can match."""

    def test_filename_stem_relaxes_to_title(self):
        from src.web_server import _audiobook_search_variants
        self.assertEqual(
            _audiobook_search_variants("sublimation - isabel j. kim (2026)"),
            ["sublimation - isabel j. kim (2026)", "sublimation - isabel j. kim", "sublimation"],
        )

    def test_extension_and_year_stripped(self):
        from src.web_server import _audiobook_search_variants
        self.assertEqual(_audiobook_search_variants("Blister (2016).epub"), ["Blister (2016).epub", "Blister"])

    def test_clean_query_is_unchanged(self):
        from src.web_server import _audiobook_search_variants
        self.assertEqual(_audiobook_search_variants("Sublimation"), ["Sublimation"])

    def test_title_with_author_suffix(self):
        from src.web_server import _audiobook_search_variants
        self.assertEqual(
            _audiobook_search_variants("The Lord of the Rings - J.R.R. Tolkien"),
            ["The Lord of the Rings - J.R.R. Tolkien", "The Lord of the Rings"],
        )


class TestEbookSearchProviderPreference(unittest.TestCase):
    """Relaxed ebook search should surface the library copy and prefer it over the local file."""

    def test_provider_upgrades_local_file(self):
        import src.web_server as ws
        from types import SimpleNamespace
        fname = "Sublimation - Isabel J. Kim (2026).epub"

        def fake_search(term):
            # Only the bare title matches BookOrbit; the filename-stem term hits only the local file.
            if term == "sublimation":
                return [SimpleNamespace(name=fname, source="BookOrbit", source_id="42")]
            return [SimpleNamespace(name=fname, source="Local File", source_id=None)]

        with patch.object(ws, "get_searchable_ebooks", side_effect=fake_search):
            out = ws._search_ebooks_with_fallback("sublimation - isabel j. kim (2026)")

        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].source, "BookOrbit")

    def test_local_file_kept_when_no_provider(self):
        import src.web_server as ws
        from types import SimpleNamespace

        def fake_search(term):
            return [SimpleNamespace(name="OnlyLocal.epub", source="Local File", source_id=None)]

        with patch.object(ws, "get_searchable_ebooks", side_effect=fake_search):
            out = ws._search_ebooks_with_fallback("only local (2020).epub")

        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].source, "Local File")


class TestForgeTextItemBuilder(unittest.TestCase):
    def test_bookorbit_source_preserves_bookorbit_id(self):
        from src.web_server import _build_forge_text_item

        item = _build_forge_text_item(
            source_type="BookOrbit",
            source_id="42",
            source_path="",
            original_filename="Book.epub",
        )

        self.assertEqual(item["source"], "BookOrbit")
        self.assertEqual(item["bookorbit_id"], "42")
        self.assertEqual(item["source_id"], "42")
        self.assertEqual(item["filename"], "Book.epub")


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
