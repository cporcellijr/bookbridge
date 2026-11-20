#!/usr/bin/env python3
"""
Regression tests for issues #609 and #610: a finished book is un-finished on a
follower.

The ebook's end and the audiobook's end do not map onto each other (back matter
is never narrated), so a finished audiobook maps to ~98.5% of the text. #610: a
Grimmory follower already at 100% was rewritten to that mapped position. #609:
with completion propagation on, the follower first took the mapped position and
its state row then replaced the 100% snapshot propagation had just persisted.

Both drive the real `SyncManager.sync_cycle()` with mocked services.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.base_sync_test import BaseSyncCycleTestCase
from src.sync_clients.sync_client_interface import LocatorResult

MAPPED_END_PCT = 0.9853656012154055  # the reporter's mapped position for a finished audiobook


class _FinishedBookCase(BaseSyncCycleTestCase):
    booklore_pct = 1.0
    booklore_prev_pct = 1.0

    def get_test_mapping(self):
        return {
            'abs_id': 'test-abs-id-finished',
            'abs_title': 'Finished Book',
            'kosync_doc_id': 'test-kosync-doc-finished',
            'ebook_filename': 'test-book.epub',
            'transcript_file': str(Path(self.temp_dir) / 'test_transcript.json'),
            'status': 'active',
        }

    def get_test_state_data(self):
        # ABS was mid-outro last cycle; this cycle it reports finished, so ABS
        # is the only client with a change and leads at 100%.
        return {
            'abs': {'pct': 0.90, 'ts': 900.0, 'last_updated': 1234567890},
            'kosync': {'pct': 1.0, 'last_updated': 1234567890},
            'booklore': {'pct': self.booklore_prev_pct, 'last_updated': 1234567890},
        }

    def get_expected_leader(self):
        return "ABS"

    def get_expected_final_percentage(self):
        return MAPPED_END_PCT

    def get_progress_mock_returns(self):
        return {
            'abs_progress': {'currentTime': 1000.0, 'duration': 1000},
            'abs_in_progress': [{'id': 'test-abs-id-finished', 'progress': 1.0, 'duration': 1000}],
            'kosync_progress': (1.0, "/body/DocFragment[40]/body/p[1]"),
            'storyteller_progress': (0.0, 0.0, None, None),
            'booklore_progress': (self.booklore_pct, None),
        }

    def _build_manager(self, propagation: bool):
        mocks = self.setup_common_mocks()
        # The last aligned words: the finished audiobook maps short of the text's end.
        mocks['ebook_parser'].find_text_location.return_value = LocatorResult(
            percentage=MAPPED_END_PCT, xpath="/body/DocFragment[38]/body/p[9]",
        )
        mocks['ebook_parser'].get_perfect_ko_xpath.return_value = "/body/DocFragment[38]/body/p[9]"

        transcriber = Mock()
        transcriber.get_text_at_time.return_value = "the last aligned words"
        transcriber.find_time_for_text.return_value = 985.0

        from src.sync_manager import SyncManager
        from src.sync_clients.abs_sync_client import ABSSyncClient
        from src.sync_clients.kosync_sync_client import KoSyncSyncClient
        from src.sync_clients.booklore_sync_client import BookloreSyncClient

        manager = SyncManager(
            abs_client=mocks['abs_client'],
            booklore_client=mocks['booklore_client'],
            transcriber=transcriber,
            ebook_parser=mocks['ebook_parser'],
            database_service=mocks['database_service'],
            sync_clients={
                "ABS": ABSSyncClient(mocks['abs_client'], transcriber, mocks['ebook_parser']),
                "KoSync": KoSyncSyncClient(mocks['kosync_client'], mocks['ebook_parser']),
                "BookLore": BookloreSyncClient(mocks['booklore_client'], mocks['ebook_parser']),
            },
            epub_cache_dir=Path(self.temp_dir) / 'epub_cache',
            data_dir=Path(self.temp_dir),
            books_dir=Path(self.temp_dir) / 'books',
        )
        manager._automatch_hardcover = Mock()
        manager._sync_to_hardcover = Mock()
        manager._mark_koreader_complete = Mock()
        manager._completion_propagation_enabled = Mock(return_value=propagation)
        manager._get_local_epub = Mock(return_value=str(Path(self.temp_dir) / 'books' / 'test-book.epub'))
        return manager, mocks

    def _saved(self, mocks, client):
        return [
            call.args[0]
            for call in mocks['database_service'].save_state.call_args_list
            if getattr(call.args[0], 'client_name', None) == client
        ]


class TestFinishedFollowerKeptFinished(_FinishedBookCase):
    """#610: ABS finished leads; Grimmory and KoSync already at 100% stay there."""

    def test_finished_followers_are_not_rewritten_to_the_mapped_position(self):
        manager, mocks = self._build_manager(propagation=False)

        with self.assertLogs('src.sync_manager', level='INFO') as logs:
            manager.sync_cycle()

        self.assertTrue(
            any("ABS leads at" in line for line in logs.output),
            f"ABS should lead this cycle; logs: {logs.output}",
        )
        mocks['booklore_client'].update_progress.assert_not_called()
        mocks['kosync_client'].update_progress.assert_not_called()
        self.assertTrue(any("Keeping 'BookLore' finished at 100.00%" in line for line in logs.output))
        self.assertFalse(
            any("Updated state data for 'BookLore'" in line for line in logs.output),
            "BookLore must not be recorded at the mapped position",
        )
        saved = self._saved(mocks, 'booklore')
        self.assertTrue(saved, "the follower's own finish should be recorded")
        self.assertAlmostEqual(float(saved[-1].percentage), 1.0, places=4)


class TestUnfinishedFollowerStillFollows(_FinishedBookCase):
    """A follower below the threshold still takes the finished leader's position."""

    booklore_pct = 0.50
    booklore_prev_pct = 0.50

    def test_unfinished_follower_gets_the_mapped_position(self):
        manager, mocks = self._build_manager(propagation=False)

        manager.sync_cycle()

        self.assertTrue(mocks['booklore_client'].update_progress.called)
        saved = self._saved(mocks, 'booklore')
        self.assertAlmostEqual(float(saved[-1].percentage), MAPPED_END_PCT, places=4)


class TestPropagatedCompletionNotOverwritten(_FinishedBookCase):
    """#609: propagation's 100% is the only write and the last state recorded."""

    booklore_pct = 0.50
    booklore_prev_pct = 0.50

    def test_propagation_target_gets_only_the_completion_write(self):
        manager, mocks = self._build_manager(propagation=True)

        with self.assertLogs('src.sync_manager', level='INFO') as logs:
            manager.sync_cycle()

        self.assertTrue(any("completion propagated to 'BookLore'" in line for line in logs.output))
        self.assertFalse(
            any("Updated state data for 'BookLore'" in line for line in logs.output),
            "the mapped follower write must not follow the propagated completion",
        )
        written = [call.args for call in mocks['booklore_client'].update_progress.call_args_list]
        self.assertEqual(len(written), 1, f"expected only the completion write, got {written}")
        saved = self._saved(mocks, 'booklore')
        self.assertAlmostEqual(float(saved[-1].percentage), 1.0, places=4)


class TestFinishedAudioFollower(unittest.TestCase):
    """A finished BookOrbit audiobook is not written back to the ebook's mapped end.

    From the owner's log: KoSync re-sent 100% on a finished book, and every cycle
    wrote `BookOrbit audio: book_id=6497 → 30749.07s (98.0%)` over the finished
    30782s, so BookOrbit showed the audiobook as re-reading. BookOrbitAudio has
    no backward-write guard of its own.
    """

    def _run(self, audio_current):
        from types import SimpleNamespace
        from tests.test_leader_normalized_ts_write import _StubClient, _base_manager, _state, _wire_database

        manager = _base_manager()
        audio = _StubClient({"audiobook"})
        kosync = _StubClient({"audiobook", "ebook"})
        manager.sync_clients = {"BookOrbitAudio": audio, "KoSync": kosync}
        manager._get_primary_audio_client_name = Mock(return_value="BookOrbitAudio")
        manager._determine_leader = Mock(return_value=("KoSync", 1.0))
        manager._resolve_alignment_locator_from_abs_timestamp = Mock(
            return_value=(LocatorResult(percentage=0.9801921756687262, match_index=386924), "anchor"),
        )
        manager._completion_propagation_enabled = Mock(return_value=False)
        manager._mark_koreader_complete = Mock()
        manager._mark_koreader_reading = Mock()
        book = SimpleNamespace(
            abs_id="bookorbit:6497", abs_title="Finished audio", status="active", duration=30782.0,
            transcript_file="DB_MANAGED", ebook_filename="book.epub", original_ebook_filename="book.epub",
            audio_source="BookOrbit", sync_mode="audiobook",
        )
        _wire_database(manager, book)
        manager._fetch_states_parallel = Mock(return_value={
            "BookOrbitAudio": _state(audio_current, previous_pct=audio_current["pct"], threshold=60.0),
            "KoSync": _state(
                {"pct": 1.0, "xpath": "/body/DocFragment[34]/body/section/p[77].0",
                 "_normalized_ts": 30749.07, "_normalization_source": "xpath"},
                # The bridge had written 98.02% to KoSync; the device re-sent 100%.
                previous_pct=0.9802, delta=0.0198, threshold=0.005,
            ),
        })
        with self.assertLogs('src.sync_manager', level='INFO') as logs:
            manager._sync_cycle_internal(target_abs_id=book.abs_id)
        return audio, logs.output

    def test_finished_audiobook_is_not_written_back_to_the_mapped_end(self):
        audio, logs = self._run({"pct": 1.0, "ts": 30782.0})

        audio.update_progress.assert_not_called()
        self.assertTrue(any(
            "Keeping 'BookOrbitAudio' finished at 100.00%: leader 'KoSync' is finished too" in line
            for line in logs
        ), logs)

    def test_audiobook_short_of_the_target_still_takes_the_forward_write(self):
        audio, _ = self._run({"pct": 0.993, "ts": 30566.0})

        audio.update_progress.assert_called_once()
        self.assertAlmostEqual(audio.update_progress.call_args[0][1].target_audio_ts, 30749.07)


if __name__ == '__main__':
    unittest.main(verbosity=2)
