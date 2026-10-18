#!/usr/bin/env python3
"""
Unit test for the "no changes detected" scenario using unittest.TestCase.
"""

import unittest
import sys
from pathlib import Path
from unittest.mock import patch
# Add the project root to the path to resolve module imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.base_sync_test import BaseSyncCycleTestCase


class TestNoChangesDetectedSync(BaseSyncCycleTestCase):
    """Test case for no changes detected sync_cycle scenario."""

    def get_test_mapping(self):
        """Return no changes test mapping configuration."""
        return {
            'abs_id': 'test-abs-id-nochange',
            'abs_title': 'No Changes Test Book',
            'kosync_doc_id': 'test-kosync-doc-nochange',
            'ebook_filename': 'test-book.epub',
            'transcript_file': str(Path(self.temp_dir) / 'test_transcript.json'),
            'status': 'active'
        }

    def get_test_state_data(self):
        """Return no changes test state data - EXACTLY matches mock returns."""
        return {
            'abs': {
                'pct': 0.25,  # 25%
                'ts': 250.0,  # timestamp
                'last_updated': 1234567890
            },
            'kosync': {
                'pct': 0.25,  # 25%
                'last_updated': 1234567890
            },
            'storyteller': {
                'pct': 0.25,  # 25%
                'last_updated': 1234567890
            },
            'booklore': {
                'pct': 0.25,  # 25%
                'last_updated': 1234567890
            }
        }

    def get_expected_leader(self):
        """Return expected leader - should be None since no changes detected."""
        return "None"

    def get_expected_final_percentage(self):
        """Return expected final percentage - should remain unchanged."""
        return 0.25  # Highest current percentage (KoSync)

    def get_progress_mock_returns(self):
        """Return progress mock return values that EXACTLY match current state."""
        return {
            'abs_progress': {'ebookProgress': 0.25, 'currentTime': 250.0, 'ebookLocation': 'epubcfi(/6/2[chapter1]!/4/2[content]/4/1:0)'},  # 25%
            'abs_in_progress': [{'id': 'test-abs-id-nochange', 'progress': 0.25, 'duration': 1000}],
            'kosync_progress': (0.25, "/html/body/div[1]/p[6]"),  # 25%
            'storyteller_progress': (0.25, 25.0, "ch2", "frag2"),  # 25%
            'booklore_progress': (0.25, None)  # 25%
        }

    def test_no_changes_detected(self):
        """Test sync_cycle when no changes are detected (all deltas are zero)."""
        log_output = super().run_test(None, None)
        self.assertNotIn("States saved to database", log_output,
                      "Logs should show no change")

    def test_instant_sync_compares_positions_on_the_audio_timeline(self):
        """Equal raw percentages can be different places: a book whose EPUB has a lot of
        unnarrated back matter. A read event's instant sync must still resolve that."""
        normalized = {"ABS": 250.0, "KoSync": 400.0}
        with patch("src.sync_manager.SyncManager._normalize_for_cross_format_comparison",
                   return_value=normalized):
            log_output = super().run_test(None, None, target_abs_id="test-abs-id-nochange")
        self.assertIn("Change detected", log_output)
        self.assertIn("KoSync leads", log_output)

    def test_instant_sync_with_a_gap_below_the_threshold_changes_nothing(self):
        """Guard: 4s apart is past the 2s deadband but under 0.5% of the 1000s book."""
        normalized = {"ABS": 250.0, "KoSync": 254.0}
        with patch("src.sync_manager.SyncManager._normalize_for_cross_format_comparison",
                   return_value=normalized):
            log_output = super().run_test(None, None, target_abs_id="test-abs-id-nochange")
        self.assertNotIn("Change detected", log_output)

    def test_instant_sync_leaves_a_small_audio_move_to_the_thresholds(self):
        """Guard: ABS moved, but by less than its threshold. The timeline gap must not
        let that small move lead and rewind the reader."""
        normalized = {"ABS": 251.4, "KoSync": 400.0}
        returns = {**self.get_progress_mock_returns(),
                   'abs_progress': {'ebookProgress': 0.25, 'currentTime': 251.4}}
        with patch("src.sync_manager.SyncManager._normalize_for_cross_format_comparison",
                   return_value=normalized), \
                patch.object(self, "get_progress_mock_returns", return_value=returns):
            log_output = super().run_test(None, None, target_abs_id="test-abs-id-nochange")
        self.assertIn("Below threshold", log_output)
        self.assertNotIn("Change detected", log_output)


if __name__ == '__main__':
    unittest.main(verbosity=2)
