"""Ownership guards on automatic audio matches and Ebook Only create (multi-user)."""

import copy
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from src.db.database_service import DatabaseService
from src.db.models import Book
from src.services.book_mapping_service import BookMappingService

HASH_A = "a" * 32
HASH_B = "b" * 32


class MappingGuardBase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        os.environ["DB_JOURNAL_MODE"] = "DELETE"
        self.db = DatabaseService(os.path.join(self.temp_dir, "guards.db"))
        self.alice = self.db.create_user("alice", "pw", role="user")
        self.bob = self.db.create_user("bob", "pw", role="user")
        self.admin = self.db.create_user("root", "pw", role="admin")
        self.db.save_book(Book(
            abs_id="audio-1", abs_title="Shared", audio_source="ABS", audio_source_id="audio-1",
            ebook_filename="a.epub", original_ebook_filename="a.epub",
            kosync_doc_id=HASH_A, status="active", sync_mode="audiobook",
        ))
        self.db.link_user_book(self.alice.id, "audio-1")
        self.svc = BookMappingService(
            database_service=self.db, booklore_client=MagicMock(), ebook_parser=MagicMock(),
            abs_client=MagicMock(), sync_clients={},
        )

    def tearDown(self):
        self.db.db_manager.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def match(self, user_id, filename="b.epub", doc=HASH_B):
        return self.svc.create_audio_mapping_from_match(
            audio_source="ABS", audio_source_id="audio-1", audio_title="Shared",
            ebook_filename=filename, kosync_doc_id=doc, user_id=user_id,
        )


class TestAutoMatchOwnership(MappingGuardBase):
    def test_second_user_auto_match_cannot_repoint_another_readers_book(self):
        self.assertIsNone(self.match(self.bob.id))
        book = self.db.get_book("audio-1")
        self.assertEqual((book.ebook_filename, book.kosync_doc_id, book.status), ("a.epub", HASH_A, "active"))
        self.assertFalse(self.db.is_user_linked(self.bob.id, "audio-1"))

    def test_second_user_auto_match_with_same_ebook_joins_without_changes(self):
        result = self.match(self.bob.id, filename="a.epub", doc=HASH_A)
        self.assertEqual(result.abs_id, "audio-1")
        self.assertTrue(self.db.is_user_linked(self.bob.id, "audio-1"))
        book = self.db.get_book("audio-1")
        self.assertEqual((book.ebook_filename, book.kosync_doc_id, book.status), ("a.epub", HASH_A, "active"))

    def test_second_user_auto_match_cannot_absorb_another_readers_ebook_only_book(self):
        self.db.save_book(Book(
            abs_id="audio-2", abs_title="Other", audio_source="ABS", audio_source_id="audio-2",
            sync_mode="audiobook", status="active",
        ))
        self.db.link_user_book(self.bob.id, "audio-2")
        self.db.save_book(Book(
            abs_id="ebook-x", abs_title="Eb", ebook_filename="x.epub", kosync_doc_id=HASH_B,
            sync_mode="ebook_only", status="active",
        ))
        self.db.link_user_book(self.alice.id, "ebook-x")
        result = self.svc.create_audio_mapping_from_match(
            audio_source="ABS", audio_source_id="audio-2", audio_title="Other",
            ebook_filename="x.epub", kosync_doc_id=HASH_B, user_id=self.bob.id,
        )
        self.assertIsNone(result)
        self.assertIsNotNone(self.db.get_book("ebook-x"))
        self.assertEqual(self.db.get_book("audio-2").status, "active")
        self.assertIsNone(self.db.get_book("audio-2").kosync_doc_id)

    def test_owner_and_admin_auto_match_still_remap(self):
        self.assertIsNotNone(self.match(self.alice.id))
        book = self.db.get_book("audio-1")
        self.assertEqual((book.ebook_filename, book.status), ("b.epub", "pending"))
        self.assertIsNotNone(self.match(self.admin.id, filename="c.epub", doc="c" * 32))
        self.assertEqual(self.db.get_book("audio-1").ebook_filename, "c.epub")

    def test_system_auto_match_without_user_still_remaps(self):
        self.assertIsNotNone(self.match(None))
        self.assertEqual(self.db.get_book("audio-1").ebook_filename, "b.epub")


class TestKosyncAutoMapRefusal(MappingGuardBase):
    def test_kosync_auto_map_refusal_for_second_user_leaves_mapping_intact(self):
        from src.api import kosync_server as ks
        saved_globals = (ks._container, ks._database_service, ks._manager)
        try:
            ks._container = SimpleNamespace(book_mapping_service=lambda: self.svc)
            ks._database_service = self.db
            ks._manager = None
            with patch.object(ks, "_resolve_library_ebook_source", return_value=(None, None)), \
                 patch.object(ks, "_resolve_abs_ebook_item", return_value=None):
                result = ks._auto_map_ebook_to_audiobook(
                    HASH_B, "b.epub",
                    {"abs_id": "audio-1", "title": "Shared", "duration": 10}, "test",
                    user_id=self.bob.id,
                )
        finally:
            ks._container, ks._database_service, ks._manager = saved_globals
        self.assertIsNone(result)
        book = self.db.get_book("audio-1")
        self.assertEqual((book.ebook_filename, book.kosync_doc_id, book.status), ("a.epub", HASH_A, "active"))


class TestEbookOnlyCreateClaim(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["DB_JOURNAL_MODE"] = "DELETE"
        self.db = DatabaseService(os.path.join(self.tmp, "eb.db"))
        self.alice = self.db.create_user("alice", "pw", role="user")
        self.bob = self.db.create_user("bob", "pw", role="user")
        self.selected = Path(self.tmp) / "edition.epub"
        self.selected.write_bytes(b"selected edition")
        self.abs_id = "ebook-" + HASH_A[:16]

    def tearDown(self):
        self.db.db_manager.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _call(self, user, existing_mode):
        from test_suggestions_redesign_api import _app_and_patches, _close
        app, web, _mock_db, patches = _app_and_patches()
        entry = {
            "key": "ebook:selected", "classification": "no_audio", "source": "Local File",
            "source_id": None, "path": str(self.selected), "filename": self.selected.name, "title": "Selected",
        }
        saved = {"ebook_discovery": {"generation": "ebook-1", "state": "complete",
                                    "entries": {entry["key"]: entry}}}
        self.db.save_book(Book(
            abs_id=self.abs_id, abs_title="Mine", ebook_filename=str(self.selected),
            kosync_doc_id=HASH_A, sync_mode=existing_mode, status="active",
            audio_source_id="aud" if existing_mode == "audiobook" else None,
        ))
        self.db.link_user_book(self.alice.id, self.abs_id)
        names = ("database_service", "_load_persisted_suggestions_cache", "_safe_local_source_path", "container")
        orig = {n: getattr(web, n) for n in names}
        try:
            web.database_service = self.db
            web._load_persisted_suggestions_cache = lambda: copy.deepcopy(saved)
            web._safe_local_source_path = lambda p: str(p) if str(p) == str(self.selected) else ""
            web.container = SimpleNamespace(ebook_parser=lambda: SimpleNamespace(get_kosync_id=lambda p: HASH_A))
            web.get_current_user_id.return_value = user.id
            return app.test_client().post("/api/suggestions/ebook-only/create", json={
                "ebook_key": "ebook:selected", "generation": "ebook-1",
            })
        finally:
            for n, v in orig.items():
                setattr(web, n, v)
            _close(patches)

    def test_ebook_only_create_claims_identical_ebook_for_second_user(self):
        resp = self._call(self.bob, "ebook_only")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["claimed"])
        self.assertEqual(body["abs_id"], self.abs_id)
        self.assertTrue(self.db.is_user_linked(self.bob.id, self.abs_id))
        self.assertEqual(self.db.get_book(self.abs_id).abs_title, "Mine")

    def test_ebook_only_create_conflicts_when_user_already_has_it(self):
        self.assertEqual(self._call(self.alice, "ebook_only").status_code, 409)

    def test_ebook_only_create_conflicts_for_audio_mapping(self):
        resp = self._call(self.bob, "audiobook")
        self.assertEqual(resp.status_code, 409)
        self.assertFalse(self.db.is_user_linked(self.bob.id, self.abs_id))


if __name__ == "__main__":
    unittest.main()
