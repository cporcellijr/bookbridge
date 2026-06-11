
import unittest
from unittest.mock import MagicMock, patch
# from pathlib import Path # Not needed if we mock objects
import os

# We verify imports work, but we will mock them in tests
from src.sync_clients.abs_sync_client import ABSSyncClient
from src.db.models import Book
from src.sync_clients.sync_client_interface import ServiceState

class TestFixSyncIssues(unittest.TestCase):
    def setUp(self):
        self.mock_abs_client = MagicMock()
        self.mock_transcriber = MagicMock()
        self.mock_ebook_parser = MagicMock()
        self.mock_alignment_service = MagicMock()
        
        self.client = ABSSyncClient(
            self.mock_abs_client,
            self.mock_transcriber,
            self.mock_ebook_parser,
            alignment_service=self.mock_alignment_service
        )

    def test_smart_fallback_missing_file_db_success(self):
        """
        Test that get_text_from_current_state falls back to DB if file is missing 
        AND alignment service finds a match.
        """
        abs_id = "test-book-id"
        book = Book(abs_id=abs_id, ebook_filename="test.epub")
        book.transcript_file = "/tmp/does_not_exist.json"
        
        # Mock State
        state = MagicMock()
        state.current = {'ts': 100.0, 'pct': 0.1}
        
        # Mock Alignment Service
        self.mock_alignment_service.get_char_for_time.return_value = 500
        
        # Mock Ebook Parser to return a MOCK path (not real Path)
        mock_book_path = MagicMock()
        mock_book_path.exists.return_value = True # Book exists!
        self.mock_ebook_parser.resolve_book_path.return_value = mock_book_path
        
        self.mock_ebook_parser.extract_text_and_map.return_value = ("A" * 1000, {}) 
        
        # Patch Path in the client module to control transcript check
        with patch('src.sync_clients.abs_sync_client.Path') as MockPath:
            # When Path(transcript) is called, return a mock that says exists=False
            mock_transcript_path = MagicMock()
            mock_transcript_path.exists.return_value = False
            MockPath.return_value = mock_transcript_path
            
            # Execute
            result = self.client.get_text_from_current_state(book, state)
            
            # Verify
            self.mock_alignment_service.get_char_for_time.assert_called_with(abs_id, 100.0)
            self.mock_ebook_parser.resolve_book_path.assert_called()
            self.mock_ebook_parser.extract_text_and_map.assert_called()
            
            expected_len = 200
            self.assertIsNotNone(result)
            self.assertEqual(len(result), expected_len)
            print(f"\n✅ Test Passed: Recovered {len(result)} chars from DB after file check failed.")

    def test_abs_progress_push_sends_zero_time_listened(self):
        """Bridge pushes are reading-driven and must not accrue ABS listening time."""
        self.mock_abs_client.update_progress.return_value = {"success": True}

        result, adjusted_ts = self.client._update_abs_progress_with_offset(
            "abs-1", 500.0, prev_abs_ts=200.0
        )

        self.assertEqual(adjusted_ts, 500.0)
        self.mock_abs_client.update_progress.assert_called_once_with("abs-1", 500.0, 0)

    def test_abs_progress_push_credits_listening_delta(self):
        """When an audio-companion leader advances the position, the forward audio
        delta is credited as listening time instead of zero."""
        self.mock_abs_client.update_progress.return_value = {"success": True}

        result, adjusted_ts = self.client._update_abs_progress_with_offset(
            "abs-1", 500.0, prev_abs_ts=200.0, time_listened=300.0
        )

        self.assertEqual(adjusted_ts, 500.0)
        self.mock_abs_client.update_progress.assert_called_once_with("abs-1", 500.0, 300.0)

    def test_update_progress_credit_listening_uses_audio_delta(self):
        """update_progress(credit_listening=True) credits ts_for_text - current ABS ts."""
        from src.sync_clients.sync_client_interface import LocatorResult, UpdateProgressRequest

        book = Book(abs_id="abs-1", ebook_filename="test.epub")
        book.transcript_file = "DB_MANAGED"
        book.duration = 1000.0

        self.mock_alignment_service.get_time_for_text.return_value = 200.0
        self.mock_abs_client.get_progress.return_value = {"currentTime": 100.0}
        self.mock_abs_client.update_progress.return_value = {"success": True}

        request = UpdateProgressRequest(
            LocatorResult(percentage=0.2, match_index=500),
            txt="anchor text",
            credit_listening=True,
        )
        self.client.update_progress(book, request)

        # delta = 200.0 (new) - 100.0 (current ABS) = 100.0 listening seconds
        self.mock_abs_client.update_progress.assert_called_once_with("abs-1", 200.0, 100.0)

    def test_update_progress_without_credit_listening_sends_zero(self):
        """Default (reading-driven) push still sends zero listening time."""
        from src.sync_clients.sync_client_interface import LocatorResult, UpdateProgressRequest

        book = Book(abs_id="abs-1", ebook_filename="test.epub")
        book.transcript_file = "DB_MANAGED"
        book.duration = 1000.0

        self.mock_alignment_service.get_time_for_text.return_value = 200.0
        self.mock_abs_client.get_progress.return_value = {"currentTime": 100.0}
        self.mock_abs_client.update_progress.return_value = {"success": True}

        request = UpdateProgressRequest(
            LocatorResult(percentage=0.2, match_index=500),
            txt="anchor text",
            credit_listening=False,
        )
        self.client.update_progress(book, request)

        self.mock_abs_client.update_progress.assert_called_once_with("abs-1", 200.0, 0)

    def test_legacy_file_used_if_exists(self):
        """
        Test that if file EXISTS, we use legacy transcriber method and do NOT call DB.
        """
        abs_id = "test-book-id-2"
        book = Book(abs_id=abs_id)
        book.transcript_file = "/tmp/exists.json"
        
        state = MagicMock()
        state.current = {'ts': 100.0}
        
        self.mock_transcriber.get_text_at_time.return_value = "Legacy Text"
        
        # Patch Path again
        with patch('src.sync_clients.abs_sync_client.Path') as MockPath:
            mock_transcript_path = MagicMock()
            mock_transcript_path.exists.return_value = True # File exists!
            MockPath.return_value = mock_transcript_path
        
            result = self.client.get_text_from_current_state(book, state)
            
            self.assertEqual(result, "Legacy Text")
            self.mock_alignment_service.get_char_for_time.assert_not_called()
            print("\n✅ Test Passed: Used legacy file when present.")

if __name__ == '__main__':
    unittest.main()
