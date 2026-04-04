#!/usr/bin/env python3
"""
Unit test for the BookLore leading scenario using unittest.TestCase.
"""

import sys
import unittest
from pathlib import Path

# Add the project root to the path to resolve module imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.base_sync_test import BaseSyncCycleTestCase


class TestBookLoreLeadsSync(BaseSyncCycleTestCase):
    """Test case for BookLore leading sync_cycle scenario."""

    def get_test_mapping(self):
        """Return BookLore test mapping configuration."""
        return {
            'abs_id': 'test-abs-id-booklore',
            'abs_title': 'BookLore Leader Test Book',
            'kosync_doc_id': 'test-kosync-doc-booklore',
            'ebook_filename': 'test-book.epub',
            'transcript_file': str(Path(self.temp_dir) / 'test_transcript.json'),
            'status': 'active'
        }

    def get_test_state_data(self):
        """Return BookLore test state data."""
        return {
            'abs': {
                'pct': 0.3,  # 30%
                'ts': 300.0,  # timestamp
                'last_updated': 1234567890
            },
            'kosync': {
                'pct': 0.4,  # 40%
                'last_updated': 1234567890
            },
            'storyteller': {
                'pct': 0.5,  # 50%
                'last_updated': 1234567890
            },
            'booklore': {
                'pct': 0.6,  # 60%
                'last_updated': 1234567890
            }
        }

    def get_expected_leader(self):
        """Return expected leader service name (client key)."""
        return "BookLore"

    def get_expected_leader_display(self):
        """Return display label used in status logs."""
        return "Grimmory"

    def get_expected_final_percentage(self):
        """Return expected final percentage."""
        return 0.75  # 75%

    def get_progress_mock_returns(self):
        """Return progress mock return values for BookLore leading scenario."""
        return {
            'abs_progress': {'ebookProgress': 0.4, 'currentTime': 400.0, 'ebookLocation': 'epubcfi(/6/8[chapter4]!/4/2[content]/12/1:0)'},  # 40%
            'abs_in_progress': [{'id': 'test-abs-id-booklore', 'progress': 0.4, 'duration': 1000}],
            'kosync_progress': (0.5, "/html/body/div[1]/p[18]"),  # 50%
            'storyteller_progress': (0.65, 65.0, "ch7", "frag7"),  # 65%
            'booklore_progress': (0.75, "/html/body/div[1]/p[25]")  # 75% - LEADER
        }

    def test_booklore_leads(self):
        super().run_test(60, 75)


if __name__ == '__main__':
    unittest.main(verbosity=2)
