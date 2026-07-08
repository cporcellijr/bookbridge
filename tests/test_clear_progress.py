"""
Unit test for the clear_progress method in SyncManager.
"""

import unittest
import logging
import os
import tempfile
import sys
from pathlib import Path
from unittest.mock import Mock, MagicMock

# Add project root to Python path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Override environment variables for testing
os.environ['DATA_DIR'] = 'test_data'
os.environ['BOOKS_DIR'] = 'test_data'

# Setup basic logging
logging.basicConfig(level=logging.INFO, format='%(levelname)s - %(message)s')


class TestClearProgressMethod(unittest.TestCase):
    """Unit test for the clear_progress method in SyncManager."""

    def setUp(self):
        """Set up test environment before each test."""
        self._old_reprocess_on_clear = os.environ.get('REPROCESS_ON_CLEAR_IF_NO_ALIGNMENT')
        os.environ['REPROCESS_ON_CLEAR_IF_NO_ALIGNMENT'] = 'true'

        # Create temporary directory for test database
        self.temp_dir = tempfile.mkdtemp()
        self.test_db_path = str(Path(self.temp_dir) / 'test_database.db')

        # Import here to avoid circular imports
        from src.db.database_service import DatabaseService
        from src.db.models import Book, State, KosyncDocument
        from src.sync_manager import SyncManager
        from src.sync_clients.sync_client_interface import SyncResult, UpdateProgressRequest, LocatorResult

        # Create database service
        self.db_service = DatabaseService(self.test_db_path)
        self.test_user = self.db_service.create_user('reader', role='admin')

        # Create test book
        self.test_book = Book(
            abs_id='test-book-123',
            abs_title='Test Book',
            ebook_filename='test-book.epub',
            status='active',
            kosync_doc_id='test-hash-123'
        )
        self.db_service.save_book(self.test_book)

        # Create test KoSync document
        self.test_doc = KosyncDocument(
            document_hash='test-hash-123',
            linked_abs_id='test-book-123',
            percentage=0.45
        )
        self.db_service.save_kosync_document(self.test_doc)
        self.db_service.upsert_user_kosync_progress(
            'test-hash-123',
            0.45,
            progress='/html/body/div[2]/p[5]',
            device='KOReader',
            device_id='device-1',
            user_id=self.test_user.id,
        )

        # Create test states for different clients
        test_states = [
            State(
                abs_id='test-book-123',
                client_name='kosync',
                last_updated=1642291200,  # Some timestamp
                percentage=0.45,  # 45%
                xpath='/html/body/div[2]/p[5]'
            ),
            State(
                abs_id='test-book-123',
                client_name='storyteller',
                last_updated=1642291200,
                percentage=0.42,  # 42%
                cfi='epubcfi(/6/4[chapter01]!/4/2/2[para05]/1:0)'
            ),
            State(
                abs_id='test-book-123',
                client_name='abs',
                last_updated=1642291200,
                percentage=0.44,  # 44%
                timestamp=12345
            )
        ]

        for state in test_states:
            self.db_service.save_state(state)

        # Create mock sync clients
        self.mock_kosync_client = Mock()
        self.mock_storyteller_client = Mock()
        self.mock_abs_client = Mock()

        # Configure mock clients
        self.mock_kosync_client.get_supported_sync_types.return_value = {'audiobook', 'ebook'}
        self.mock_kosync_client.supports_book.return_value = True
        self.mock_storyteller_client.get_supported_sync_types.return_value = {'audiobook', 'ebook'}
        self.mock_storyteller_client.supports_book.return_value = True
        self.mock_abs_client.get_supported_sync_types.return_value = {'audiobook'}
        self.mock_abs_client.supports_book.return_value = True
        self.mock_kosync_client.update_progress.return_value = SyncResult(success=True, location=0.0)
        self.mock_storyteller_client.update_progress.return_value = SyncResult(success=True, location=0.0)
        self.mock_abs_client.update_progress.return_value = SyncResult(success=True, location=0.0)

        # Create sync manager with mocked dependencies
        self.sync_manager = SyncManager(
            abs_client=Mock(),
            booklore_client=Mock(),
            transcriber=Mock(),
            ebook_parser=Mock(),
            database_service=self.db_service,
            sync_clients={
                'kosync': self.mock_kosync_client,
                'storyteller': self.mock_storyteller_client,
                'abs': self.mock_abs_client
            },
            data_dir=Path(self.temp_dir),
            books_dir=Path(self.temp_dir) / 'books'
        )

    def tearDown(self):
        """Clean up after each test."""
        import shutil
        if self._old_reprocess_on_clear is None:
            os.environ.pop('REPROCESS_ON_CLEAR_IF_NO_ALIGNMENT', None)
        else:
            os.environ['REPROCESS_ON_CLEAR_IF_NO_ALIGNMENT'] = self._old_reprocess_on_clear
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_clear_progress_success(self):
        """Test successful clearing of progress for a book."""
        # Verify initial state - should have 3 state records
        initial_states = self.db_service.get_states_for_book('test-book-123')
        self.assertEqual(len(initial_states), 3, "Should start with 3 state records")
        
        # Verify KoSync document exists
        self.assertIsNotNone(self.db_service.get_kosync_document('test-hash-123'))

        # Verify initial progress values
        state_by_client = {state.client_name: state for state in initial_states}
        self.assertAlmostEqual(state_by_client['kosync'].percentage, 0.45, places=2)
        self.assertAlmostEqual(state_by_client['storyteller'].percentage, 0.42, places=2)
        self.assertAlmostEqual(state_by_client['abs'].percentage, 0.44, places=2)

        # Call clear_progress
        result = self.sync_manager.clear_progress('test-book-123')

        # Verify return value structure
        self.assertIsInstance(result, dict)
        self.assertIn('book_id', result)
        self.assertIn('book_title', result)
        self.assertIn('database_states_cleared', result)
        self.assertIn('client_reset_results', result)
        self.assertIn('successful_resets', result)
        self.assertIn('total_clients', result)

        # Verify correct values in result
        self.assertEqual(result['book_id'], 'test-book-123')
        self.assertEqual(result['book_title'], 'Test Book')
        self.assertEqual(result['database_states_cleared'], 3)
        self.assertEqual(result['total_clients'], 3)
        self.assertEqual(result['successful_resets'], 3)

        # Verify all clients were reset
        self.assertEqual(len(result['client_reset_results']), 3)
        for client_name in ['kosync', 'storyteller', 'abs']:
            self.assertIn(client_name, result['client_reset_results'])
            self.assertTrue(result['client_reset_results'][client_name]['success'])
            self.assertEqual(result['client_reset_results'][client_name]['message'], 'Reset to 0%')

        # Verify database states were replaced by explicit 0% reset snapshots.
        remaining_states = self.db_service.get_states_for_book('test-book-123')
        self.assertEqual(len(remaining_states), 3, "Successful resets should leave 0% state snapshots")
        for state in remaining_states:
            self.assertAlmostEqual(state.percentage, 0.0)
            self.assertEqual(state.service_updated_at, state.last_updated)

        # Verify user-scoped KoSync device progress was zeroed so sibling-hash
        # furthest-wins cannot resurrect the old position on the next GET.
        user_progress = self.db_service.get_user_kosync_progress('test-hash-123', self.test_user.id)
        self.assertIsNotNone(user_progress)
        self.assertAlmostEqual(float(user_progress.percentage), 0.0)
        self.assertEqual(user_progress.progress, "")

        # Verify KoSync document was deleted
        self.assertIsNone(self.db_service.get_kosync_document('test-hash-123'), "KoSync document should be deleted")

        # Verify book status was updated to 'pending'
        updated_book = self.db_service.get_book('test-book-123')
        self.assertEqual(updated_book.status, 'pending', "Book status should be 'pending' for re-sync")

        # Verify sync clients were called correctly
        from src.sync_clients.sync_client_interface import UpdateProgressRequest, LocatorResult

        for mock_client in [self.mock_kosync_client, self.mock_storyteller_client, self.mock_abs_client]:
            mock_client.update_progress.assert_called_once()
            call_args = mock_client.update_progress.call_args

            # Verify the book argument
            self.assertEqual(call_args[0][0].abs_id, 'test-book-123')

            # Verify the UpdateProgressRequest argument
            request = call_args[0][1]
            self.assertIsInstance(request, UpdateProgressRequest)
            self.assertEqual(request.locator_result.percentage, 0.0)
            self.assertEqual(request.txt, "")
            self.assertIsNone(request.previous_location)

    def test_clear_progress_nonexistent_book(self):
        """Test clearing progress for a book that doesn't exist."""
        with self.assertRaises(RuntimeError) as context:
            self.sync_manager.clear_progress('nonexistent-book-456')

        self.assertIn('Book not found: nonexistent-book-456', str(context.exception))

    def test_clear_progress_with_client_failures(self):
        """Test clearing progress when some clients fail to reset."""
        # Make storyteller client fail
        from src.sync_clients.sync_client_interface import SyncResult
        self.mock_storyteller_client.update_progress.return_value = SyncResult(success=False, location=None)

        # Call clear_progress
        result = self.sync_manager.clear_progress('test-book-123')

        # Verify database was still cleared
        self.assertEqual(result['database_states_cleared'], 3)
        remaining_states = self.db_service.get_states_for_book('test-book-123')
        self.assertEqual(len(remaining_states), 2)
        self.assertEqual({s.client_name for s in remaining_states}, {'kosync', 'abs'})

        # Verify partial success
        self.assertEqual(result['successful_resets'], 2)  # kosync and abs succeeded
        self.assertEqual(result['total_clients'], 3)

        # Verify individual client results
        client_results = result['client_reset_results']
        self.assertTrue(client_results['kosync']['success'])
        self.assertFalse(client_results['storyteller']['success'])  # This one failed
        self.assertTrue(client_results['abs']['success'])

    def test_scoped_clear_progress_resets_user_kosync_progress_without_deleting_document(self):
        """Per-user resets must zero user hash progress while preserving the shared document link."""
        result = self.sync_manager.clear_progress('test-book-123', user_id=self.test_user.id)

        self.assertEqual(result['kosync_progress_rows_reset'], 1)
        self.assertIsNotNone(self.db_service.get_kosync_document('test-hash-123'))

        user_progress = self.db_service.get_user_kosync_progress('test-hash-123', self.test_user.id)
        self.assertIsNotNone(user_progress)
        self.assertAlmostEqual(float(user_progress.percentage), 0.0)
        self.assertEqual(user_progress.progress, "")

    def test_clear_progress_with_client_exception(self):
        """Test clearing progress when a client raises an exception."""
        # Make kosync client raise an exception
        self.mock_kosync_client.update_progress.side_effect = Exception("Connection error")

        # Call clear_progress
        result = self.sync_manager.clear_progress('test-book-123')

        # Verify database was still reset for the clients that succeeded.
        self.assertEqual(result['database_states_cleared'], 3)
        remaining_states = self.db_service.get_states_for_book('test-book-123')
        self.assertEqual(len(remaining_states), 2)
        self.assertEqual({s.client_name for s in remaining_states}, {'storyteller', 'abs'})

        # Verify partial success (2 out of 3 clients succeeded)
        self.assertEqual(result['successful_resets'], 2)
        self.assertEqual(result['total_clients'], 3)

        # Verify the exception was handled properly
        client_results = result['client_reset_results']
        self.assertFalse(client_results['kosync']['success'])
        self.assertIn('Connection error', client_results['kosync']['message'])

    def test_clear_progress_skips_unconfigured_clients(self):
        """Unconfigured per-user clients must not be called during reset."""
        from src.sync_clients.sync_client_interface import SyncResult

        configured = Mock()
        configured.is_configured.return_value = True
        configured.get_supported_sync_types.return_value = {'audiobook', 'ebook'}
        configured.supports_book.return_value = True
        configured.update_progress.return_value = SyncResult(success=True, location=0.0)

        unconfigured = Mock()
        unconfigured.is_configured.return_value = False
        unconfigured.get_supported_sync_types.return_value = {'audiobook', 'ebook'}
        unconfigured.supports_book.return_value = True

        result = self.sync_manager.clear_progress(
            'test-book-123',
            sync_clients={
                'Configured': configured,
                'Hardcover': unconfigured,
            },
        )

        configured.update_progress.assert_called_once()
        unconfigured.update_progress.assert_not_called()
        self.assertEqual(result['total_clients'], 1)
        self.assertEqual(result['database_reset_snapshots_saved'], 1)
        self.assertIn('Configured', result['client_reset_results'])
        self.assertNotIn('Hardcover', result['client_reset_results'])


if __name__ == '__main__':
    unittest.main()
