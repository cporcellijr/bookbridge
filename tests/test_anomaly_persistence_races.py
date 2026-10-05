"""SQLite regressions for persistence races reported by the anomaly receiver."""

import os
import shutil
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import event, inspect

from src.db.database_service import DatabaseService
from src.db.models import BookloreBook, UserCredential


class TestCredentialInsertRace(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "race.db")
        self.db = DatabaseService(self.db_path)
        self.user = self.db.create_user("race-user", "password")

    def tearDown(self):
        self.db.db_manager.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_concurrent_insert_winner_is_updated_without_losing_credential_contract(self):
        """A competing writer inserts after our miss; the upsert must still win."""
        injected = False

        def insert_competing_credential(conn, cursor, statement, parameters, context, executemany):
            nonlocal injected
            if injected or not statement.lstrip().upper().startswith("INSERT INTO USER_CREDENTIALS"):
                return
            injected = True
            with sqlite3.connect(self.db_path) as other_connection:
                other_connection.execute(
                    'INSERT INTO user_credentials (user_id, "key", value) VALUES (?, ?, ?)',
                    (self.user.id, "LLM_API_KEY", "enc:v1:competing-writer"),
                )
                other_connection.commit()

        event.listen(self.db.db_manager.engine, "before_cursor_execute", insert_competing_credential)
        try:
            result = self.db.set_user_credential(self.user.id, "LLM_API_KEY", "current-secret")
        finally:
            event.remove(self.db.db_manager.engine, "before_cursor_execute", insert_competing_credential)

        self.assertTrue(injected, "the test must insert between the SELECT miss and write")
        self.assertIsInstance(result, UserCredential)
        self.assertEqual(result.user_id, self.user.id)
        self.assertEqual(result.key, "LLM_API_KEY")
        self.assertEqual(result.value, "current-secret")
        self.assertTrue(inspect(result).detached)
        with sqlite3.connect(self.db_path) as connection:
            stored = connection.execute(
                'SELECT id, value FROM user_credentials WHERE user_id = ? AND "key" = ?',
                (self.user.id, "LLM_API_KEY"),
            ).fetchone()
        self.assertEqual(stored[0], result.id)
        self.assertTrue(stored[1].startswith("enc:v1:"))
        self.assertNotEqual(stored[1], "current-secret")

    def test_two_workers_racing_first_write_keep_user_scopes_separate(self):
        """Both workers miss before writing; SQLite conflict handling serializes them."""
        other_user = self.db.create_user("other-race-user", "password")
        both_missed = threading.Barrier(2)

        def synchronize_first_writes(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith("INSERT INTO USER_CREDENTIALS"):
                both_missed.wait(timeout=5)

        event.listen(self.db.db_manager.engine, "before_cursor_execute", synchronize_first_writes)
        try:
            with ThreadPoolExecutor(max_workers=2) as workers:
                results = list(workers.map(
                    lambda value: self.db.set_user_credential(
                        self.user.id, "ABS_ENABLED", value
                    ),
                    ("true", "false"),
                ))
        finally:
            event.remove(self.db.db_manager.engine, "before_cursor_execute", synchronize_first_writes)

        self.assertEqual({result.user_id for result in results}, {self.user.id})
        self.assertEqual({result.key for result in results}, {"ABS_ENABLED"})
        self.assertEqual(len({result.id for result in results}), 1)
        self.assertIn(
            self.db.get_user_credential(self.user.id, "ABS_ENABLED"), {"true", "false"}
        )
        self.db.set_user_credential(other_user.id, "ABS_ENABLED", "other-user-value")
        self.assertEqual(
            self.db.get_user_credential(other_user.id, "ABS_ENABLED"), "other-user-value"
        )
        self.assertIn(
            self.db.get_user_credential(self.user.id, "ABS_ENABLED"), {"true", "false"}
        )


class TestBookloreCacheEvictionRace(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db = DatabaseService(os.path.join(self.tmpdir, "race.db"))

    def tearDown(self):
        self.db.db_manager.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_cache_row_evicted_after_lookup_is_inserted_again(self):
        """A real SQLite eviction between ORM lookup and update must not stale-fail."""
        original = self.db.save_booklore_book(
            BookloreBook(filename="cached.epub", title="Old title", authors="Old author")
        )
        evicted = False

        def evict_before_update(conn, cursor, statement, parameters, context, executemany):
            nonlocal evicted
            if evicted or not statement.lstrip().upper().startswith("UPDATE BOOKLORE_BOOKS"):
                return
            evicted = True
            with sqlite3.connect(self.db.db_manager.db_path) as other_connection:
                other_connection.execute(
                    "DELETE FROM booklore_books WHERE filename = ?", ("cached.epub",)
                )
                other_connection.commit()

        event.listen(self.db.db_manager.engine, "before_cursor_execute", evict_before_update)
        try:
            saved = self.db.save_booklore_book(
                BookloreBook(
                    filename="cached.epub",
                    title="Updated title",
                    authors="Updated author",
                    raw_metadata='{"id": 7}',
                )
            )
        finally:
            event.remove(self.db.db_manager.engine, "before_cursor_execute", evict_before_update)

        self.assertTrue(evicted, "the test must evict the row after the cache lookup")
        self.assertIsNone(original.raw_metadata)
        self.assertIsNotNone(saved.id)
        self.assertEqual(saved.filename, "cached.epub")
        self.assertEqual(saved.title, "Updated title")
        self.assertEqual(saved.authors, "Updated author")
        self.assertEqual(saved.raw_metadata, '{"id": 7}')
        stored = self.db.get_booklore_book("cached.epub")
        self.assertEqual(stored.id, saved.id)
        self.assertEqual(stored.title, "Updated title")
        self.assertEqual(stored.raw_metadata_dict, {"id": 7})


if __name__ == "__main__":
    unittest.main()
