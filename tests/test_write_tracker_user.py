import unittest

from src.services import write_tracker
from src.utils import user_context


class TestWriteTrackerUserScoping(unittest.TestCase):
    def setUp(self):
        write_tracker._recent_writes.clear()

    def tearDown(self):
        write_tracker._recent_writes.clear()

    def test_suppression_is_per_user(self):
        write_tracker.record_write("ABS", "book1", user_id=1)
        # same client+book but a different user is NOT suppressed
        self.assertTrue(write_tracker.is_own_write("ABS", "book1", user_id=1))
        self.assertFalse(write_tracker.is_own_write("ABS", "book1", user_id=2))

    def test_backward_compatible_no_user(self):
        write_tracker.record_write("KoSync", "book9")
        self.assertTrue(write_tracker.is_own_write("KoSync", "book9"))
        # a user-scoped check for the same book is independent
        self.assertFalse(write_tracker.is_own_write("KoSync", "book9", user_id=5))

    def test_recent_write_metadata_carries_pct(self):
        write_tracker.record_write("Storyteller", "b", pct=0.42, user_id=7)
        meta = write_tracker.get_recent_write("Storyteller", "b", user_id=7)
        self.assertIsNotNone(meta)
        self.assertEqual(meta["pct"], 0.42)

    def test_record_resolves_ambient_sync_user(self):
        # A client deep in the sync push records without threading user_id; the
        # write must still key on the cycle's user so only that user's reader
        # (poller/socket) suppresses it.
        token = user_context.set_current_user_id(3)
        try:
            write_tracker.record_write("ABS", "bookX")
        finally:
            user_context.reset_current_user_id(token)
        self.assertTrue(write_tracker.is_own_write("ABS", "bookX", user_id=3))
        self.assertFalse(write_tracker.is_own_write("ABS", "bookX", user_id=4))


if __name__ == "__main__":
    unittest.main()
