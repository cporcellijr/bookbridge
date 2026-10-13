"""Progress-write log, built-in KoSync URL detection and the bulk-state contract."""

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.db.database_service import DatabaseService
from src.db.models import Book, KosyncDocument, State
from src.sync_clients.bookfusion_sync_client import BookFusionSyncClient
from src.sync_clients.storyteller_sync_client import StorytellerSyncClient
from src.utils.kosync_url import is_builtin_kosync_url


class TestProgressWriteLog(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.db = DatabaseService(str(Path(self.temp_dir) / "writes.db"))
        self.db.save_book(Book(abs_id="b1", abs_title="One", status="active", kosync_doc_id="hash-1"))
        self.db.save_book(Book(abs_id="b2", abs_title="Two", status="active"))
        self.db.create_user("admin", "pw", role="admin")
        self.uid = self.db._resolve_uid(None)

    def tearDown(self) -> None:
        self.db.db_manager.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _assert_bumps(self, abs_id: str, write) -> None:
        seq = self.db.progress_write_seq()
        self.assertFalse(self.db.progress_written_since(abs_id, seq))
        write()
        self.assertTrue(self.db.progress_written_since(abs_id, seq))
        self.assertFalse(self.db.progress_written_since("unrelated", seq))

    def test_save_state_bumps_only_that_book(self) -> None:
        self._assert_bumps("b1", lambda: self.db.save_state(
            State(abs_id="b1", client_name="kosync", percentage=0.5, last_updated=1.0)))

    def test_user_kosync_progress_bumps_the_book_the_hash_belongs_to(self) -> None:
        self._assert_bumps("b1", lambda: self.db.upsert_user_kosync_progress("hash-1", 0.4, progress="/p"))

    def test_user_kosync_progress_bumps_the_linked_book(self) -> None:
        self.db.save_kosync_document(KosyncDocument(document_hash="hash-2", linked_abs_id="b2", percentage=0))
        self._assert_bumps("b2", lambda: self.db.upsert_user_kosync_progress("hash-2", 0.4, progress="/p"))

    def test_document_progress_write_bumps_the_linked_book(self) -> None:
        self._assert_bumps("b2", lambda: self.db.save_kosync_document(
            KosyncDocument(document_hash="hash-3", linked_abs_id="b2", percentage=0.3, progress="/p")))

    def test_clearing_progress_bumps(self) -> None:
        self._assert_bumps("b1", lambda: self.db.reset_user_kosync_progress_for_book("b1"))
        self._assert_bumps("b1", lambda: self.db.delete_states_for_book("b1"))

    def test_rolled_back_write_does_not_bump(self) -> None:
        seq = self.db.progress_write_seq()
        with self.assertRaises(RuntimeError), patch.object(self.db, "_resolve_uid", return_value=self.uid):
            with self.db.get_session() as session:
                self.db._mark_progress_written(session, "b1")
                raise RuntimeError("boom")
        self.assertFalse(self.db.progress_written_since("b1", seq))


class TestBuiltinKosyncUrl(unittest.TestCase):
    def test_port_semantics(self) -> None:
        with patch.dict("os.environ", {"KOSYNC_PORT": "5758"}):
            self.assertTrue(is_builtin_kosync_url("http://127.0.0.1:5757"))
            self.assertTrue(is_builtin_kosync_url("http://localhost:5758/koreader"))
            self.assertFalse(is_builtin_kosync_url("http://127.0.0.1:9999"))
            self.assertFalse(is_builtin_kosync_url("http://192.168.1.5:5757"))
            self.assertFalse(is_builtin_kosync_url(""))
            # A port-less URL means the scheme's default port, never the bridge's
            self.assertFalse(is_builtin_kosync_url("http://127.0.0.1"))
        with patch.dict("os.environ", {"KOSYNC_PORT": "80"}):
            self.assertTrue(is_builtin_kosync_url("http://127.0.0.1"))
            self.assertFalse(is_builtin_kosync_url("https://127.0.0.1"))


class TestBulkStateContract(unittest.TestCase):
    def test_bookfusion_failed_page_is_a_failed_read(self) -> None:
        client = MagicMock()
        client.search_books.side_effect = [[{"id": i, "reading_position": {"percentage": 1}} for i in range(100)], None]
        self.assertIsNone(BookFusionSyncClient(client, MagicMock()).fetch_bulk_state())

    def test_storyteller_empty_read_is_a_failed_read(self) -> None:
        st = MagicMock()
        st.get_all_positions_bulk.return_value = {}
        self.assertIsNone(StorytellerSyncClient(st, MagicMock(), MagicMock()).fetch_bulk_state())
        st.get_all_positions_bulk.return_value = {"t": {"pct": 0.1}}
        self.assertEqual(StorytellerSyncClient(st, MagicMock(), MagicMock()).fetch_bulk_state(), {"t": {"pct": 0.1}})


if __name__ == "__main__":
    unittest.main()
