"""A non-admin's match must not change or delete another user's mapping."""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src.db.database_service import DatabaseService
from src.db.models import Book, KosyncDocument, State
from src.utils.user_context import reset_current_user_id, set_current_user_id
from tests.test_match_paths_regression import MockContainer

HASH = "0123456789abcdef0123456789abcdef"
_ENV_KEYS = ("DATA_DIR", "BOOKS_DIR", "TEMPLATE_DIR")


class TestCrossUserMappingGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._saved_env = {key: os.environ.get(key) for key in _ENV_KEYS}
        os.environ["DATA_DIR"] = self.tmp
        os.environ["BOOKS_DIR"] = self.tmp
        os.environ["TEMPLATE_DIR"] = str(Path(__file__).parent.parent / "templates")

        self.svc = DatabaseService(os.path.join(self.tmp, "probe.db"))
        self.admin = self.svc.create_user("admin", "secret", role="admin")
        self.alice = self.svc.create_user("alice", "pw", role="user")
        self.bob = self.svc.create_user("bob", "pw", role="user")

        # Alice's audiobook mapping holds the ebook's hash, with her progress.
        self.svc.save_book(Book(
            abs_id="ab-alice", abs_title="Shared Ebook", ebook_filename="book.epub",
            kosync_doc_id=HASH, sync_mode="audiobook", status="active",
        ))
        self.svc.link_user_book(self.alice.id, "ab-alice")
        self.svc.save_state(State(
            abs_id="ab-alice", client_name="abs", user_id=self.alice.id,
            percentage=0.42, timestamp=1234.0, last_updated=1.0,
        ))
        self.svc.save_kosync_document(KosyncDocument(
            document_hash=HASH, linked_abs_id="ab-alice", user_id=self.alice.id,
        ))

        self.container = MockContainer()
        self.container.mock_database_service = self.svc
        self.container.mock_abs_client.get_all_audiobooks.return_value = [
            {"id": abs_id, "media": {"metadata": {"title": "Shared Ebook", "authorName": "A"}, "duration": 3600}}
            for abs_id in ("ab-bob", "ab-alice")
        ]

        import src.db.migration_utils
        self._orig_init = src.db.migration_utils.initialize_database
        src.db.migration_utils.initialize_database = lambda _d: self.svc

        from src.web_server import create_app
        self.app, _ = create_app(test_container=self.container)
        self.app.config["TESTING"] = True
        self.app.config["LOGIN_DISABLED"] = False
        self.client = self.app.test_client()

    def tearDown(self):
        import src.db.migration_utils
        src.db.migration_utils.initialize_database = self._orig_init
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _login(self, username, password="pw"):
        self.client.post("/login", data={"username": username, "password": password})

    @patch("src.web_server.get_kosync_id_for_ebook", return_value=HASH)
    def test_non_admin_match_leaves_other_users_mapping(self, _hash):
        self._login("bob")
        self.assertFalse(self.svc.is_user_linked(self.bob.id, "ab-alice"))

        r = self.client.post("/match", data={"audiobook_id": "ab-bob", "ebook_filename": "book.epub"})

        self.assertEqual(r.status_code, 403)
        self.assertIsNotNone(self.svc.get_book("ab-alice"))
        self.assertTrue(self.svc.is_user_linked(self.alice.id, "ab-alice"))
        self.assertEqual(self.svc.get_states_for_book("ab-bob", user_id=self.alice.id), [])
        self.assertIsNone(self.svc.get_book("ab-bob"))

    @patch("src.web_server.get_kosync_id_for_ebook", return_value="f" * 32)
    def test_non_admin_rematch_leaves_other_users_mapping(self, _hash):
        self._login("bob")

        r = self.client.post("/match", data={"audiobook_id": "ab-alice", "ebook_filename": "other.epub"})

        self.assertEqual(r.status_code, 403)
        book = self.svc.get_book("ab-alice")
        self.assertEqual(book.ebook_filename, "book.epub")
        self.assertEqual(book.status, "active")
        self.assertFalse(self.svc.is_user_linked(self.bob.id, "ab-alice"))

    @patch("src.web_server.get_kosync_id_for_ebook", return_value=HASH)
    def test_non_admin_same_text_joins_without_changing(self, _hash):
        self._login("bob")

        r = self.client.post("/match", data={"audiobook_id": "ab-alice", "ebook_filename": "book.epub"})

        self.assertEqual(r.status_code, 302)
        self.assertTrue(self.svc.is_user_linked(self.bob.id, "ab-alice"))
        self.assertTrue(self.svc.is_user_linked(self.alice.id, "ab-alice"))
        book = self.svc.get_book("ab-alice")
        self.assertEqual(book.ebook_filename, "book.epub")
        self.assertEqual(book.status, "active")
        (state,) = self.svc.get_states_for_book("ab-alice", user_id=self.alice.id)
        self.assertAlmostEqual(state.percentage, 0.42)

    @patch("src.web_server.get_kosync_id_for_ebook", return_value=HASH)
    def test_admin_may_still_merge(self, _hash):
        self._login("admin", "secret")

        r = self.client.post("/match", data={"audiobook_id": "ab-bob", "ebook_filename": "book.epub"})

        self.assertNotEqual(r.status_code, 403)
        self.assertIsNone(self.svc.get_book("ab-alice"))
        self.assertIsNotNone(self.svc.get_book("ab-bob"))

    @patch("src.web_server.get_kosync_id_for_ebook", return_value="f" * 32)
    def test_owner_may_still_rematch_own_mapping(self, _hash):
        self._login("alice")

        r = self.client.post("/match", data={"audiobook_id": "ab-alice", "ebook_filename": "other.epub"})

        self.assertNotEqual(r.status_code, 403)
        self.assertEqual(self.svc.get_book("ab-alice").ebook_filename, "other.epub")

    @patch("src.web_server.get_kosync_id_for_ebook", return_value=HASH)
    def test_non_admin_library_audio_match_cannot_absorb_other_users_mapping(self, _hash):
        self._login("bob")

        r = self.client.post("/match", data={
            "audio_source": "BookOrbit", "audio_source_id": "bo-1",
            "audio_title": "Shared Ebook", "ebook_filename": "book.epub",
        })

        self.assertEqual(r.status_code, 403)
        self.assertIsNotNone(self.svc.get_book("ab-alice"))
        self.assertTrue(self.svc.is_user_linked(self.alice.id, "ab-alice"))
        self.assertIsNone(self.svc.get_book_by_audio_source("BookOrbit", "bo-1"))

    @patch("src.web_server.get_kosync_id_for_ebook", return_value=HASH)
    def test_non_admin_audio_only_match_cannot_strip_other_users_text(self, _hash):
        self._login("bob")

        r = self.client.post("/match", data={
            "audiobook_id": "ab-alice", "audio_only": "true", "audio_source": "ABS",
            "audio_source_id": "ab-alice", "audio_title": "Shared Ebook",
        })

        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.svc.get_book("ab-alice").ebook_filename, "book.epub")
        self.assertFalse(self.svc.is_user_linked(self.bob.id, "ab-alice"))

    def _give_bob_a_book(self):
        self.svc.save_book(Book(
            abs_id="ab-bob", abs_title="Bob's Book", ebook_filename="bob.epub",
            kosync_doc_id="b" * 32, sync_mode="audiobook", status="active",
        ))
        self.svc.link_user_book(self.bob.id, "ab-bob")
        # Saving outside a request claims the book for the default admin too, which
        # would make it "shared" and trip the admin-only shared-hash rule instead.
        self.svc.unlink_user_book(self.admin.id, "ab-bob")

    def test_non_admin_cannot_take_another_mappings_hash_through_update_hash(self):
        self._give_bob_a_book()
        self._login("bob")

        r = self.client.post("/update-hash/ab-bob", data={"new_hash": HASH})

        self.assertEqual(r.status_code, 403)
        self.assertIn("another reader's mapping", r.get_data(as_text=True))
        self.assertEqual(self.svc.get_kosync_document(HASH).linked_abs_id, "ab-alice")
        self.assertEqual(self.svc.get_book("ab-bob").kosync_doc_id, "b" * 32)

    def test_update_hash_still_sets_an_unowned_hash(self):
        self._give_bob_a_book()
        self._login("bob")

        r = self.client.post("/update-hash/ab-bob", data={"new_hash": "e" * 32})

        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.svc.get_book("ab-bob").kosync_doc_id, "e" * 32)

    @patch("src.web_server.get_kosync_id_for_ebook", return_value="c" * 32)
    def test_joining_a_bookorbit_mapping_gives_the_joiner_a_link(self, _hash):
        self.svc.save_book(Book(
            abs_id="bookorbit:bo-1", abs_title="Orbit Book", ebook_filename="orbit.epub",
            kosync_doc_id="c" * 32, sync_mode="audiobook", status="active",
            audio_source="BookOrbit", audio_source_id="bo-1", audio_provider_book_id="bo-1",
        ))
        self.svc.link_user_book(self.alice.id, "bookorbit:bo-1")
        self._login("bob")

        r = self.client.post("/match", data={
            "audio_source": "BookOrbit", "audio_source_id": "bo-1",
            "audio_title": "Orbit Book", "ebook_filename": "orbit.epub",
        })

        self.assertEqual(r.status_code, 302)
        self.assertTrue(self.svc.is_user_linked(self.bob.id, "bookorbit:bo-1"))
        link = self.svc.get_user_bookorbit_link(self.bob.id, "bookorbit:bo-1")
        self.assertEqual((link or {}).get("audio_id"), "bo-1")

    def test_storyteller_fallback_never_overwrites_a_cached_artifact(self):
        import src.web_server as ws

        cache = Path(self.tmp) / "epub_cache"
        cache.mkdir()
        cached = cache / "storyteller_victim-uuid.epub"
        cached.write_bytes(b"victim's readalong")
        library = Path(self.tmp) / "storyteller_library" / "Other Book"
        library.mkdir(parents=True)
        (library / "other readaloud.epub").write_bytes(b"someone else's book")

        clients = Mock()
        clients.storyteller_client.download_slim_book.return_value = False
        container = Mock()
        container.epub_cache_dir.return_value = cache
        with patch.object(ws, "uc", return_value=clients), patch.object(ws, "container", container), \
                patch.dict(os.environ, {"STORYTELLER_LIBRARY_DIR": str(library.parent)}):
            filename, path = ws._download_storyteller_artifact("victim-uuid", "Other Book")

        self.assertEqual(filename, "storyteller_victim-uuid.epub")
        self.assertEqual(cached.read_bytes(), b"victim's readalong")
        clients.storyteller_client._strip_audio_from_epub.assert_not_called()

    def test_background_job_uses_the_user_it_runs_for(self):
        import src.web_server as ws

        expected = {self.bob.id: False, self.alice.id: True, self.admin.id: True, None: True}
        with patch.object(ws, "database_service", self.svc):
            for user_id, allowed in expected.items():
                with self.subTest(user_id=user_id):
                    token = set_current_user_id(user_id)
                    try:
                        self.assertIs(ws._acting_user_may_modify_book("ab-alice"), allowed)
                    finally:
                        reset_current_user_id(token)


if __name__ == "__main__":
    unittest.main()
