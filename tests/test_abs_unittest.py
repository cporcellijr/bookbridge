#!/usr/bin/env python3
"""
Unit test for the ABS leading scenario using unittest.TestCase.
"""

import unittest
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
# Add the project root to the path to resolve module imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.base_sync_test import BaseSyncCycleTestCase
from src.sync_clients.kosync_sync_client import KoSyncSyncClient
from src.sync_clients.sync_client_interface import LocatorResult

class TestABSLeadsSync(BaseSyncCycleTestCase):
    """Test case for ABS leading sync_cycle scenario."""

    def get_test_mapping(self):
        """Return ABS test mapping configuration."""
        return {
            'abs_id': 'test-abs-id-123',
            'abs_title': 'Test Audiobook',
            'kosync_doc_id': 'test-kosync-doc',
            'ebook_filename': 'test-book.epub',
            'transcript_file': str(Path(self.temp_dir) / 'test_transcript.json'),
            'status': 'active',
            'duration': 1000.0  # 1000 seconds test duration
        }

    def get_test_state_data(self):
        """Return ABS test state data."""
        return {
            'abs': {
                'pct': 0.1,  # 10%
                'ts': 100.0,  # timestamp
                'last_updated': 1234567890
            },
            'kosync': {
                'pct': 0.2,  # 20%
                'last_updated': 1234567890
            },
            'storyteller': {
                'pct': 0.1,  # 10%
                'last_updated': 1234567890
            },
            'booklore': {
                'pct': 0.0,  # 0%
                'last_updated': 1234567890
            },
            'hardcover': {
                'pct': 1.0,  # 100% - highest progress but should be excluded
                'last_updated': 1234567890
            }
        }

    def get_expected_leader(self):
        """Return expected leader service name."""
        return "ABS"

    def get_expected_final_percentage(self):
        """Return expected final percentage."""
        return 0.4  # 40%

    def get_progress_mock_returns(self):
        """Return progress mock return values for ABS leading scenario."""
        return {
            'abs_progress': {'ebookProgress': 0.32035211267605633805, 'currentTime': 400.0, 'ebookLocation': 'epubcfi(/6/10[Afscheid_voor_even-2]!/4[Afscheid_voor_even-2]/2[book-columns]/2[book-inner]/2/230/4/2[kobo.115.2]/1:29)'},  # 40%
            'abs_in_progress': [{'id': 'test-abs-id-123', 'progress': 0.4, 'duration': 1000}],
            'kosync_progress': (0.2, "/html/body/div[1]/p[5]"),  # 20%
            'storyteller_progress': (0.1, 10.0, "ch1", "frag1"),  # 10%
            'booklore_progress': (0.0, None),  # 0%
            'hardcover_progress': (1.0, {'status_id': 2, 'page_number': 350, 'total_pages': 350})  # 100% - should be excluded
        }

    def test_abs_leads(self):
        super().run_test(10, 40)

    def test_malformed_xpath_skips_kosync_update(self):
        kosync_api = Mock()
        ebook_parser = Mock()
        ebook_parser.get_sentence_level_ko_xpath.return_value = None
        client = KoSyncSyncClient(kosync_api, ebook_parser)

        book = SimpleNamespace(
            kosync_doc_id='test-kosync-doc',
            ebook_filename='test-book.epub',
            abs_title='Test Audiobook',
        )
        request = SimpleNamespace(
            locator_result=LocatorResult(percentage=0.4, xpath="/html/body/div[1]/p[5]")
        )

        result = client.update_progress(book, request)

        self.assertFalse(result.success)
        self.assertTrue(result.updated_state.get('skipped'))
        self.assertIsNone(result.updated_state.get('xpath'))
        kosync_api.update_progress.assert_not_called()

    def test_kosync_state_includes_recent_external_put_metadata(self):
        kosync_api = Mock()
        kosync_api.get_progress_with_metadata.return_value = (
            0.441,
            "/body/DocFragment[31]/body/div[1].0",
            {
                "_bridge_recent_external_put": True,
                "_bridge_recent_external_put_device": "Kobo_monza",
                "_bridge_recent_external_put_device_id": "device-id",
                "_bridge_recent_external_put_age_seconds": 247.0,
            },
        )
        kosync_api.is_configured.return_value = True
        ebook_parser = Mock()
        client = KoSyncSyncClient(kosync_api, ebook_parser)

        book = SimpleNamespace(kosync_doc_id='test-kosync-doc')
        state = client.get_service_state(book, prev_state=None, title_snip="Test Audiobook")

        self.assertTrue(state.current["_kosync_recent_external_put"])
        self.assertEqual(state.current["_kosync_last_put_device"], "Kobo_monza")
        self.assertEqual(state.current["_kosync_last_put_device_id"], "device-id")
        self.assertEqual(state.current["_kosync_last_put_age_seconds"], 247.0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
