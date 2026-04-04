
import unittest
from unittest.mock import Mock, MagicMock
import os
import sys
from pathlib import Path

# Add project root to Python path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.sync_manager import SyncManager

class TestSuggestionLogic(unittest.TestCase):
    def setUp(self):
        self.mock_db = Mock()
        self.mock_db.get_books_by_status.return_value = []
        self.mock_db.get_all_books.return_value = []
        self.mock_db.get_all_pending_suggestions.return_value = []
        
        self.mock_abs = Mock()
        self.mock_booklore = Mock()
        
        self.manager = SyncManager(
            database_service=self.mock_db,
            abs_client=self.mock_abs,
            booklore_client=self.mock_booklore,
            sync_clients={},
            data_dir=Path('/tmp')
        )
        
        # Default environment to enabled
        os.environ['SUGGESTIONS_ENABLED'] = 'true'

    def tearDown(self):
        if 'SUGGESTIONS_ENABLED' in os.environ:
            del os.environ['SUGGESTIONS_ENABLED']

    def test_suggestion_ignored_when_progress_high(self):
        """Test that suggestions are NOT created if progress > 70%."""
        # Setup
        abs_id = "book-123"
        progress_data = {
            "duration": 1000,
            "currentTime": 750  # 75%
        }
        
        # Mocks
        self.mock_db.get_all_books.return_value = [] # Not mapped
        self.mock_db.get_pending_suggestion.return_value = None # No pending suggestion
        self.mock_db.suggestion_exists.return_value = False # No dismissed suggestion (simulating clean state)
        
        # Action
        self.manager.check_for_suggestions({abs_id: progress_data}, [])
        
        # Assert
        # Should NOT call save_pending_suggestion because 75% > 70%
        self.mock_db.save_pending_suggestion.assert_not_called()

    def test_suggestion_created_when_progress_low(self):
        """Test that suggestions ARE created if progress < 70% (and > 1%)."""
        # Setup
        abs_id = "book-456"
        progress_data = {
            "duration": 1000,
            "currentTime": 500  # 50%
        }
        
        # Mocks
        self.mock_db.get_all_books.return_value = [] 
        self.mock_db.get_pending_suggestion.return_value = None
        self.mock_db.suggestion_exists.return_value = False
        
        # Prepare successful suggestion creation mocks
        self.mock_abs.get_item_details.return_value = {
            'media': {'metadata': {'title': 'Test Book', 'authorName': 'Author'}}
        }
        self.mock_booklore.is_configured.return_value = True
        self.mock_booklore.search_books.return_value = [{'title': 'Test Book', 'fileName': 'test.epub'}]
        
        # Action
        self.manager.check_for_suggestions({abs_id: progress_data}, [])
        
        # Assert
        self.mock_db.save_pending_suggestion.assert_called_once()

    def test_suggestion_ignored_when_dismissed(self):
        """Test that suggestions are NOT created if they were previously dismissed."""
        # Setup
        abs_id = "book-789"
        progress_data = {
            "duration": 1000,
            "currentTime": 500  # 50%
        }
        
        # Mocks
        self.mock_db.get_all_books.return_value = []
        self.mock_db.get_pending_suggestion.return_value = None # No *active* pending suggestion
        self.mock_db.suggestion_exists.return_value = True # BUT it exists (likely dismissed)
        
        # Action
        self.manager.check_for_suggestions({abs_id: progress_data}, [])
        
        # Assert
        self.mock_db.save_pending_suggestion.assert_not_called()

if __name__ == '__main__':
    unittest.main()
