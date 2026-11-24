"""The Grimmory cache table is read once and reused until the mixin writes it.

Measured live: loading 1,428 rows took ~120 ms on every Library and book-window
view on the 9p /data mount.
"""

import shutil
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from src.db.database_service import DatabaseService
from src.db.models import BookloreBook


class TestBookloreBooksSnapshot(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db = DatabaseService(str(Path(self.temp_dir) / "booklore_snapshot.db"))
        self.db.save_booklore_book(BookloreBook("a.epub", title="A"))

    def tearDown(self):
        self.db.db_manager.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _insert_behind_the_mixin(self, filename):
        with self.db.db_manager.engine.begin() as conn:
            conn.exec_driver_sql("INSERT INTO booklore_books (filename) VALUES (?)", (filename,))

    def _names(self):
        return sorted(b.filename for b in self.db.get_all_booklore_books())

    def test_repeated_reads_reuse_the_first_load(self):
        self.assertEqual(self._names(), ["a.epub"])
        self._insert_behind_the_mixin("hidden.epub")
        self.assertEqual(self._names(), ["a.epub"])

    def test_every_mixin_write_drops_the_snapshot(self):
        self._names()
        self._insert_behind_the_mixin("hidden.epub")
        self.db.save_booklore_book(BookloreBook("b.epub"))
        self.assertEqual(self._names(), ["a.epub", "b.epub", "hidden.epub"])

        self.db.delete_booklore_book("b.epub")
        self.assertEqual(self._names(), ["a.epub", "hidden.epub"])

        self.db.clear_all_booklore_books()
        self.assertEqual(self._names(), [])

    def test_a_read_during_a_write_does_not_keep_pre_commit_rows(self):
        self._names()
        real_get_session = self.db.get_session
        fired = []

        @contextmanager
        def session_with_a_concurrent_read():
            if not fired:
                fired.append(True)
                # Another thread loads after the write's invalidation but before its commit.
                self.db.get_all_booklore_books()
            with real_get_session() as session:
                yield session

        with patch.object(self.db, "get_session", session_with_a_concurrent_read):
            self.db.save_booklore_book(BookloreBook("a.epub", title="New"))

        self.assertEqual([b.title for b in self.db.get_all_booklore_books()], ["New"])

    def test_callers_get_their_own_list(self):
        first = self.db.get_all_booklore_books()
        first.clear()
        self.assertEqual(self._names(), ["a.epub"])


if __name__ == "__main__":
    unittest.main()
