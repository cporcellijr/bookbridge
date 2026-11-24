#!/usr/bin/env python3
"""
Regression coverage for #468: write-only tracker posts that fail must not be
retried on every sync cycle.

The reporter's Hardcover account logged ~1,000 API requests overnight with no
reading at all. A book Hardcover could not take a post for (no match, no usable
edition, missing from the account) never recorded a post, so once its idle
cooldown settled every sync cycle re-ran the whole automatch search. Failed posts
now back off exponentially; new progress still gets a fresh attempt.

Also covers two Hardcover bugs found alongside it: the re-read candidate was
dropped by the cooldown post (so a re-read could never be confirmed), and the
"no valid edition" sentinel was checked only after spending a get_user_book call.
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.api.hardcover_client import HardcoverClient
from src.db.database_service import DatabaseService
from src.db.models import Book, HardcoverDetails
from src.sync_clients.hardcover_sync_client import (
    REREAD_CANDIDATE_PCT_KEY,
    HardcoverSyncClient,
)
from src.sync_clients.sync_client_interface import LocatorResult, SyncResult, UpdateProgressRequest
from src.sync_manager import (
    _TRACKER_RETRY_BASE_SECONDS,
    _TRACKER_RETRY_MAX_SECONDS,
    SyncManager,
)

CYCLE_SECONDS = 300  # SYNC_PERIOD_MINS default of 5

TRACKERS = {
    # client_key: (handler name, cooldown env, store attr, lock attr)
    'Hardcover': ('_handle_hardcover_cooldown', 'HARDCOVER_UPDATE_COOLDOWN_MINS',
                  '_hardcover_cooldown', '_hardcover_cooldown_lock'),
    'StoryGraph': ('_handle_storygraph_cooldown', 'STORYGRAPH_UPDATE_COOLDOWN_MINS',
                   '_storygraph_cooldown', '_storygraph_cooldown_lock'),
    'Goodreads': ('_handle_goodreads_cooldown', 'GOODREADS_UPDATE_COOLDOWN_MINS',
                  '_goodreads_cooldown', '_goodreads_cooldown_lock'),
}


class FakeServiceState:
    def __init__(self, pct):
        self.current = {'pct': pct}


class FakeBook:
    def __init__(self, abs_id):
        self.abs_id = abs_id


class FakeDatabaseService:
    def __init__(self):
        self.states = {}

    def get_state(self, abs_id, client_name):
        return self.states.get((abs_id, client_name))

    def save_state(self, state):
        self.states[(state.abs_id, state.client_name)] = state
        return state


class ScriptedTrackerClient:
    """Tracker client whose update_progress outcomes are scripted per call."""

    def __init__(self, outcomes=None):
        self.outcomes = list(outcomes or [])
        self.calls = []  # percentages posted

    def is_configured(self):
        return True

    def update_progress(self, book, request):
        self.calls.append(request.locator_result.percentage)
        outcome = self.outcomes.pop(0) if self.outcomes else False
        if isinstance(outcome, Exception):
            raise outcome
        if outcome:
            return SyncResult(request.locator_result.percentage, True)
        return SyncResult(None, False)


def _bare_manager(sync_clients, database_service):
    mgr = SyncManager.__new__(SyncManager)
    mgr.sync_clients = sync_clients
    mgr.database_service = database_service
    for _, _, store_attr, lock_attr in TRACKERS.values():
        setattr(mgr, store_attr, {})
        setattr(mgr, lock_attr, threading.Lock())
    mgr._tracker_cooldown_timers = {}
    mgr._tracker_cooldown_timers_lock = threading.Lock()
    return mgr


class _EnvMixin:
    _ENV_KEYS = (
        'HARDCOVER_ENABLED', 'HARDCOVER_TOKEN', 'HARDCOVER_UPDATE_COOLDOWN_MINS',
        'STORYGRAPH_UPDATE_COOLDOWN_MINS', 'GOODREADS_UPDATE_COOLDOWN_MINS',
    )

    def _save_env(self):
        saved = {key: os.environ.get(key) for key in self._ENV_KEYS}

        def restore():
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.addCleanup(restore)


class TestIdleUnmatchedBookStopsHittingHardcover(_EnvMixin, unittest.TestCase):
    """#468: an idle book with no Hardcover match, through the real client stack."""

    TITLE = 'Some Niche Book'

    def setUp(self):
        self._save_env()
        os.environ['HARDCOVER_ENABLED'] = 'true'
        os.environ['HARDCOVER_TOKEN'] = 'hc_pat_test'
        os.environ['HARDCOVER_UPDATE_COOLDOWN_MINS'] = '240'  # the reporter's value

        self.temp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.temp_dir, True)
        self.db = DatabaseService(str(Path(self.temp_dir) / 'test_468.db'))
        self.book = Book(abs_id='ebook-468', abs_title=self.TITLE,
                         ebook_filename='niche.epub', status='active')
        self.db.save_book(self.book)

        ids_patch = patch(
            'src.sync_clients.hardcover_sync_client.resolve_ebook_identifiers',
            return_value={'title': self.TITLE, 'author': 'A. Writer', 'isbn': '9780000000001'},
        )
        ids_patch.start()
        self.addCleanup(ids_patch.stop)

        self.post = patch('src.api.hardcover_client.requests.post', side_effect=self._fake_post).start()
        self.addCleanup(patch.stopall)

        hardcover = HardcoverSyncClient(HardcoverClient(), Mock(), abs_client=None,
                                        database_service=self.db)
        self.mgr = _bare_manager({'Hardcover': hardcover}, self.db)

    @staticmethod
    def _fake_post(url, json=None, **kwargs):
        response = MagicMock()
        response.status_code = 200
        if 'search(' in json['query']:
            response.json.return_value = {'data': {'search': {'ids': []}}}
        else:
            response.json.return_value = {'data': {'editions': []}}
        return response

    def test_twelve_idle_hours_make_a_handful_of_requests_not_hundreds(self):
        config = {'KoSync': FakeServiceState(0.42)}
        self.mgr._handle_hardcover_cooldown(self.book, config, 0.0, schedule_followup=False)
        settled_at = 240 * 60

        with self.assertLogs('src.sync_clients.hardcover_sync_client', level='WARNING') as logs:
            for cycle in range(12 * 3600 // CYCLE_SECONDS):
                self.mgr._handle_hardcover_cooldown(
                    self.book, config, settled_at + cycle * CYCLE_SECONDS, schedule_followup=False,
                )

        no_match = [line for line in logs.output
                    if f"⚠️ Hardcover: No match found for '{self.TITLE}'" in line]
        # Attempts at +0, +15m, +45m, +1h45m, +3h45m, +7h45m — not 144 (one per cycle).
        self.assertEqual(len(no_match), 6)
        self.assertEqual(self.post.call_count, 6 * 3)  # ISBN + title/author + title searches


class TestTrackerPostFailureBackoff(_EnvMixin, unittest.TestCase):
    """The backoff lives in the shared handler, so every tracker gets it."""

    def setUp(self):
        self._save_env()
        self.book = FakeBook('book1')

    def _setup(self, client_key, outcomes, cooldown_mins='0'):
        handler_name, env_key, _, _ = TRACKERS[client_key]
        os.environ[env_key] = cooldown_mins
        client = ScriptedTrackerClient(outcomes)
        db = FakeDatabaseService()
        mgr = _bare_manager({client_key: client}, db)
        handler = getattr(mgr, handler_name)
        return client, db, handler

    def _run(self, client, handler, pct, start, end):
        """Run one handler call per sync cycle; return the times a post was attempted."""
        attempts = []
        for now in range(start, end, CYCLE_SECONDS):
            before = len(client.calls)
            handler(self.book, {'ABS': FakeServiceState(pct)}, float(now), schedule_followup=False)
            if len(client.calls) > before:
                attempts.append(now)
        return attempts

    def test_failed_post_backs_off_exponentially_up_to_the_cap(self):
        expected, t, delay = [], 0, _TRACKER_RETRY_BASE_SECONDS
        while len(expected) < 9:
            expected.append(t)
            t += int(delay)
            delay = min(delay * 2, _TRACKER_RETRY_MAX_SECONDS)
        for client_key in TRACKERS:
            with self.subTest(tracker=client_key):
                client, _, handler = self._setup(client_key, outcomes=[])
                attempts = self._run(client, handler, 0.5, 0, expected[-1] + CYCLE_SECONDS)
                self.assertEqual(attempts, expected)

    def test_new_progress_gets_a_fresh_attempt_inside_the_backoff(self):
        for client_key in TRACKERS:
            with self.subTest(tracker=client_key):
                client, _, handler = self._setup(client_key, outcomes=[False, True])
                handler(self.book, {'ABS': FakeServiceState(0.5)}, 0.0, schedule_followup=False)
                handler(self.book, {'ABS': FakeServiceState(0.55)}, 300.0, schedule_followup=False)
                self.assertEqual(client.calls, [0.5, 0.55])

    def test_success_after_a_failure_records_the_post_and_stops(self):
        client, db, handler = self._setup('Hardcover', outcomes=[False, True])
        attempts = self._run(client, handler, 0.5, 0, 6 * 3600)
        self.assertEqual(attempts, [0, int(_TRACKER_RETRY_BASE_SECONDS)])
        self.assertAlmostEqual(db.get_state('book1', 'hardcover').percentage, 0.5)

    def test_a_raised_exception_is_backed_off_too(self):
        client, _, handler = self._setup('Hardcover', outcomes=[RuntimeError('boom')])
        with self.assertLogs('src.sync_manager', level='WARNING') as logs:
            attempts = self._run(client, handler, 0.5, 0, int(_TRACKER_RETRY_BASE_SECONDS))
        self.assertEqual(attempts, [0])
        self.assertTrue(any('Hardcover cooldown handler failed' in line for line in logs.output))

    def test_failed_completion_post_is_backed_off(self):
        client, _, handler = self._setup('Hardcover', outcomes=[], cooldown_mins='60')
        attempts = self._run(client, handler, 1.0, 0, int(_TRACKER_RETRY_BASE_SECONDS) + CYCLE_SECONDS)
        self.assertEqual(attempts, [0, int(_TRACKER_RETRY_BASE_SECONDS)])

    def test_failure_logs_the_retry_decision_at_info(self):
        _, _, handler = self._setup('Hardcover', outcomes=[False])
        with self.assertLogs('src.sync_manager', level='INFO') as logs:
            handler(self.book, {'ABS': FakeServiceState(0.5)}, 0.0, schedule_followup=False)
        self.assertIn(
            "⏸️ 'book1' Hardcover post failed at 50.0%; retrying in 15m (attempt 1)",
            '\n'.join(logs.output),
        )


class TestHardcoverRereadSurvivesCooldownPost(_EnvMixin, unittest.TestCase):
    """The re-read candidate must persist between cooldown posts to be confirmed."""

    ABS_ID = 'reread-book'

    def setUp(self):
        self._save_env()
        os.environ['HARDCOVER_UPDATE_COOLDOWN_MINS'] = '0'
        self.temp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.temp_dir, True)
        self.db = DatabaseService(str(Path(self.temp_dir) / 'test_reread_cooldown.db'))
        self.book = Book(abs_id=self.ABS_ID, abs_title='A Book Worth Rereading',
                         ebook_filename='reread.epub', status='active')
        self.db.save_book(self.book)
        self.db.save_hardcover_details(HardcoverDetails(
            abs_id=self.ABS_ID, hardcover_book_id='book-1', hardcover_edition_id='edition-1',
            hardcover_pages=200, matched_by='test',
        ))

        self.api = Mock()
        self.api.is_configured.return_value = True
        self.api.update_progress.return_value = True
        self.api.get_user_book.return_value = {'id': 'user-book-1', 'status_id': 3}
        self.api.get_latest_read.return_value = {
            'id': 'completed-read-1', 'started_at': '2026-07-01', 'finished_at': '2026-07-05',
        }
        self.hardcover = HardcoverSyncClient(hardcover_client=self.api, ebook_parser=Mock(),
                                             abs_client=Mock(), database_service=self.db)
        self.mgr = _bare_manager({'Hardcover': self.hardcover}, self.db)

    def _post(self, pct, now):
        self.mgr._handle_hardcover_cooldown(self.book, {'KoSync': FakeServiceState(pct)},
                                            now, schedule_followup=False)

    def _allow_new_read_flags(self):
        return [c.kwargs.get('allow_new_read') for c in self.api.update_progress.call_args_list]

    def test_reread_is_confirmed_across_two_cooldown_posts(self):
        self._post(0.04, 0.0)
        stored = json.loads(self.db.get_state(self.ABS_ID, 'hardcover').locator_json)
        self.assertEqual(stored[REREAD_CANDIDATE_PCT_KEY], 0.04)

        self._post(0.09, 300.0)

        self.assertEqual(self._allow_new_read_flags(), [False, True])
        self.assertEqual(self.hardcover._read_reread_candidate(self.book), (None, None))

    def test_stale_low_position_never_confirms(self):
        self._post(0.04, 0.0)
        self._post(0.05, 300.0)
        self.assertEqual(self._allow_new_read_flags(), [False, False])


class TestHardcoverNoEditionSentinel(unittest.TestCase):
    """A book already verified to have no usable edition costs no API call."""

    def _client(self, pages, audio_seconds=None):
        api = Mock()
        api.is_configured.return_value = True
        api.get_user_book.return_value = {'id': 'ub-1', 'status_id': 2}
        api.get_latest_read.return_value = {'id': 'read-1', 'finished_at': None}
        api.update_progress.return_value = True
        db = Mock()
        db.get_hardcover_details.return_value = HardcoverDetails(
            abs_id='b1', hardcover_book_id='book-1', hardcover_edition_id='ed-1',
            hardcover_pages=pages, hardcover_audio_seconds=audio_seconds, matched_by='test',
        )
        book = Book(abs_id='b1', abs_title='No Edition', status='active')
        return HardcoverSyncClient(api, Mock(), database_service=db), api, book

    def _request(self, pct=0.5):
        return UpdateProgressRequest(locator_result=LocatorResult(percentage=pct))

    def test_no_edition_sentinel_skips_get_user_book(self):
        client, api, book = self._client(pages=-1)
        result = client.update_progress(book, self._request())
        self.assertFalse(result.success)
        api.get_user_book.assert_not_called()

    def test_audiobook_edition_with_page_sentinel_still_syncs(self):
        client, api, book = self._client(pages=-1, audio_seconds=36000)
        result = client.update_progress(book, self._request())
        self.assertTrue(result.success)
        api.get_user_book.assert_called_once()


if __name__ == '__main__':
    unittest.main(verbosity=2)
