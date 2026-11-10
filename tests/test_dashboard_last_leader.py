"""Tests for the Dashboard 'last instigator' indicator.

Covers the data path that lets the 'In Progress' cards subtly dot whichever
service last moved a book's position:

1. ``DatabaseService.get_all_reading_stats`` surfaces ``last_leader`` — the
   ``leader_client`` of the most recent ``ReadingSession`` per book — using
   SQLite's max()/bare-column guarantee (verified with out-of-order inserts).
2. ``_dashboard_leader_service`` normalizes a raw ``leader_client`` to the
   lowercase service key the template dots (device/audio collapse, tracker None).
"""

import os
import sys
import shutil
import tempfile
import unittest
from pathlib import Path

# Add project root to Python path
sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault('DATA_DIR', 'test_data')
os.environ.setdefault('BOOKS_DIR', 'test_data')


class TestReadingStatsLastLeader(unittest.TestCase):
    """get_all_reading_stats reports the leader of the newest session per book."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.test_db_path = str(Path(self.temp_dir) / 'test_database.db')

        from src.db.database_service import DatabaseService
        from src.db.models import Book

        self.Book = Book
        self.db_service = DatabaseService(self.test_db_path)

    def tearDown(self):
        if hasattr(self, 'db_service') and hasattr(self.db_service, 'db_manager'):
            self.db_service.db_manager.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_last_leader_is_leader_of_most_recent_session(self):
        """The reported last_leader must come from the greatest end_time row,
        not insertion/id order — so the newer session is inserted FIRST."""
        user = self.db_service.create_user('rl-user', 'pw', role='admin')
        abs_id = 'rl-book'
        self.db_service.save_book(self.Book(abs_id=abs_id, abs_title='RL', status='active'))

        # Newer session (end_time 5000, ABS) inserted first; older (end_time
        # 1000, KoSync) inserted last. Insertion/id order would wrongly pick KoSync.
        self.db_service.record_reading_session(
            abs_id=abs_id, session_type='AUDIOBOOK', start_time=4900.0, end_time=5000.0,
            duration_seconds=100, start_progress=0.5, end_progress=0.6,
            leader_client='ABS', user_id=user.id,
        )
        self.db_service.record_reading_session(
            abs_id=abs_id, session_type='EPUB', start_time=900.0, end_time=1000.0,
            duration_seconds=100, start_progress=0.1, end_progress=0.2,
            leader_client='KoSync:kindle', user_id=user.id,
        )

        stats = self.db_service.get_all_reading_stats(user_id=user.id)
        self.assertIn(abs_id, stats)
        self.assertEqual(stats[abs_id]['last_leader'], 'ABS')
        self.assertEqual(stats[abs_id]['last_session_time'], 5000.0)
        self.assertEqual(stats[abs_id]['session_count'], 2)

    def test_last_leader_is_scoped_per_user(self):
        """Each user's last_leader reflects only their own sessions."""
        user_a = self.db_service.create_user('rl-a', 'pw', role='admin')
        user_b = self.db_service.create_user('rl-b', 'pw', role='user')
        abs_id = 'rl-shared'
        self.db_service.save_book(self.Book(abs_id=abs_id, abs_title='Shared', status='active'))

        # User A read earlier via ABS; user B read later via KoSync. Scoped per
        # user, A must still see ABS even though B's session is globally newer.
        self.db_service.record_reading_session(
            abs_id=abs_id, session_type='AUDIOBOOK', start_time=100.0, end_time=200.0,
            duration_seconds=100, leader_client='ABS', user_id=user_a.id,
        )
        self.db_service.record_reading_session(
            abs_id=abs_id, session_type='EPUB', start_time=300.0, end_time=400.0,
            duration_seconds=100, leader_client='KoSync', user_id=user_b.id,
        )

        stats_a = self.db_service.get_all_reading_stats(user_id=user_a.id)
        stats_b = self.db_service.get_all_reading_stats(user_id=user_b.id)
        self.assertEqual(stats_a[abs_id]['last_leader'], 'ABS')
        self.assertEqual(stats_b[abs_id]['last_leader'], 'KoSync')


class TestDashboardLeaderServiceNormalization(unittest.TestCase):
    """_dashboard_leader_service maps raw leader_client to a template service key."""

    def test_known_leaders_map_to_service_keys(self):
        from src.web_server import _dashboard_leader_service as normalize

        cases = {
            'KoSync': 'kosync',
            'KoSync:kindle-abc': 'kosync',
            'BridgeSync_Plugin': 'kosync',
            'ABS': 'abs',
            'ABSEbook': 'abs',
            'Storyteller': 'storyteller',
            'BookLore': 'booklore',
            'BookLoreAudio': 'bookloreaudio',
            'BookOrbit': 'bookorbit',
            'BookOrbitAudio': 'bookorbitaudio',
            'CWA': 'cwa',
            # Case-insensitive.
            'kosync': 'kosync',
            'bookloreaudio': 'bookloreaudio',
        }
        for raw, expected in cases.items():
            self.assertEqual(normalize(raw), expected, f"{raw!r} should map to {expected!r}")

    def test_unknown_and_tracker_leaders_get_no_dot(self):
        from src.web_server import _dashboard_leader_service as normalize

        for value in (None, '', '   ', 'Hardcover', 'StoryGraph', 'BookFusion',
                      'Readest', 'SomethingNew'):
            self.assertIsNone(normalize(value), f"{value!r} should not resolve to a dot")



class TestFormatStrip(unittest.TestCase):
    """The library card's format strip: which slot each leader lights, and the slots' states."""

    def test_leaders_map_to_format_slots(self):
        from src.web_server import _format_strip_slot

        cases = {
            'ABS': 'audio', 'BookOrbitAudio': 'audio', 'BookLoreAudio': 'audio',
            'ABSEbook': 'ebook', 'BookOrbit': 'ebook', 'CWA': 'ebook', 'Kavita': 'ebook', 'BookFusion': 'ebook',
            'KoSync:kindle': 'koreader', 'BridgeSync_Plugin': 'koreader', 'Storyteller': 'readalong',
            'Hardcover': None, 'StoryGraph': None, None: None, '': None,
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(_format_strip_slot(raw), expected)

    def test_slots_show_links_the_last_leader_and_the_disagreeing_position(self):
        from src.web_server import _build_format_strip

        slots = {slot['key']: slot for slot in _build_format_strip({
            'states': {
                'bookorbit': {'percentage': 48.0, 'last_updated': 2},
                'bookorbitaudio': {'percentage': 48.0},
                'kosync': {'percentage': 61.0},
            },
            'ebook_source': 'BookOrbit', 'audio_source': 'BookOrbit', 'sync_mode': 'audiobook',
            'last_sync_leader': 'BookOrbit', 'is_out_of_sync': True, 'unified_progress': 61.0,
        })}

        self.assertEqual(list(slots), ['ebook', 'audio', 'readalong', 'koreader'])
        self.assertEqual((slots['ebook']['service'], slots['ebook']['pct'], slots['ebook']['last']), ('BookOrbit', 48.0, True))
        self.assertEqual((slots['audio']['linked'], slots['audio']['last']), (True, False))
        self.assertFalse(slots['readalong']['linked'])
        self.assertTrue(slots['koreader']['oos'])
        self.assertEqual([key for key, slot in slots.items() if slot['oos']], ['koreader'])

    def test_generated_readalong_lights_its_slot_and_ebook_only_has_no_audio(self):
        from src.web_server import _build_format_strip

        slots = {slot['key']: slot for slot in _build_format_strip({
            'states': {}, 'sync_mode': 'ebook_only', 'ebook_source': 'CWA', 'readalong_ready': True,
        })}

        self.assertEqual((slots['ebook']['linked'], slots['ebook']['service']), (True, 'Calibre-Web'))
        self.assertFalse(slots['audio']['linked'])
        self.assertEqual((slots['readalong']['linked'], slots['readalong']['service']), (True, None))
        self.assertFalse(any(slot['last'] or slot['oos'] for slot in slots.values()))


if __name__ == '__main__':
    unittest.main()
