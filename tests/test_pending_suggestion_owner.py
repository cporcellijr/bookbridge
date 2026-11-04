"""Pending suggestions are owned per reader (multi-user)."""

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from flask import Flask

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.db.database_service import DatabaseService
from src.db.models import Book, PendingSuggestion
from src.services.shelf_watch_service import ShelfWatchService
from src.services.suggestions_service import SuggestionsService
from src.utils.user_context import reset_current_user_id, set_current_user_id


def _suggestion(source_id="src-1", title="Book", **kw) -> PendingSuggestion:
    return PendingSuggestion(source_id=source_id, title=title, matches_json=json.dumps([{"x": 1}]), **kw)


class OwnerDbBase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        os.environ["DB_JOURNAL_MODE"] = "DELETE"
        self.db = DatabaseService(os.path.join(self.temp_dir, "owner.db"))
        self.alice = self.db.create_user("alice", "pw", role="user")
        self.bob = self.db.create_user("bob", "pw", role="user")
        self.admin = self.db.create_user("root", "pw", role="admin")

    def tearDown(self):
        self.db.db_manager.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def save(self, uid, source_id="src-1", **kw):
        return self.db.save_pending_suggestion(_suggestion(source_id, **kw), user_id=uid)


class TestOwnerScoping(OwnerDbBase):
    def test_ignore_by_one_reader_does_not_hide_it_for_another(self):
        self.save(self.alice.id)
        self.save(self.bob.id)
        self.assertTrue(self.db.ignore_suggestion("src-1", user_id=self.alice.id))
        self.assertEqual(self.db.get_ignored_suggestion_source_ids(user_id=self.alice.id), ["src-1"])
        self.assertEqual(self.db.get_ignored_suggestion_source_ids(user_id=self.bob.id), [])
        self.assertEqual([s.source_id for s in self.db.get_all_pending_suggestions(user_id=self.bob.id)], ["src-1"])
        self.assertEqual(self.db.get_all_pending_suggestions(user_id=self.alice.id), [])

    def test_same_source_id_can_be_pending_for_two_readers(self):
        a = self.save(self.alice.id, title="A")
        b = self.save(self.bob.id, title="B")
        self.assertNotEqual(a.id, b.id)
        self.assertEqual(self.db.get_pending_suggestion("src-1", user_id=self.alice.id).title, "A")
        self.assertEqual(self.db.get_pending_suggestion("src-1", user_id=self.bob.id).title, "B")
        again = self.save(self.alice.id, title="A2")
        self.assertEqual(again.id, a.id)
        self.assertEqual(again.user_id, self.alice.id)
        self.assertEqual(self.db.get_pending_suggestion("src-1", user_id=self.bob.id).title, "B")

    def test_dismiss_and_clear_stale_touch_only_the_owner(self):
        self.save(self.alice.id)
        self.save(self.bob.id)
        self.assertTrue(self.db.dismiss_suggestion("src-1", user_id=self.alice.id))
        self.assertIsNone(self.db.get_pending_suggestion("src-1", user_id=self.alice.id))
        self.assertIsNotNone(self.db.get_pending_suggestion("src-1", user_id=self.bob.id))
        self.assertEqual(self.db.clear_stale_suggestions(user_id=self.alice.id), 1)
        self.assertTrue(self.db.suggestion_exists("src-1", user_id=self.bob.id))
        self.assertFalse(self.db.suggestion_exists("src-1", user_id=self.alice.id))
        self.assertFalse(self.db.dismiss_suggestion("src-1", user_id=self.alice.id))

    def test_legacy_null_owner_rows_belong_to_the_default_user(self):
        self.db.save_pending_suggestion(_suggestion("legacy"), user_id=self.admin.id)
        with self.db.get_session() as session:
            session.query(PendingSuggestion).update({PendingSuggestion.user_id: None})
        self.assertEqual(len(self.db.get_all_pending_suggestions(user_id=self.admin.id)), 1)
        self.assertEqual(self.db.get_all_pending_suggestions(user_id=self.alice.id), [])
        self.assertFalse(self.db.suggestion_exists("legacy", user_id=self.bob.id))
        self.assertTrue(self.db.ignore_suggestion("legacy", user_id=self.admin.id))
        self.assertEqual(self.db.get_ignored_suggestion_source_ids(user_id=self.admin.id), ["legacy"])
        resaved = self.save(self.admin.id, "legacy")
        self.assertEqual(resaved.user_id, self.admin.id)

    def test_ambient_user_scopes_when_no_explicit_id(self):
        token = set_current_user_id(self.bob.id)
        try:
            self.db.save_pending_suggestion(_suggestion("amb"))
        finally:
            reset_current_user_id(token)
        self.assertTrue(self.db.suggestion_exists("amb", user_id=self.bob.id))
        self.assertFalse(self.db.suggestion_exists("amb", user_id=self.alice.id))

    def test_all_users_lists_every_owner(self):
        self.save(self.alice.id, "a")
        self.save(self.bob.id, "b")
        self.assertEqual({s.source_id for s in self.db.get_all_pending_suggestions(all_users=True)}, {"a", "b"})


class TestMigration(unittest.TestCase):
    def _module(self):
        source = Path(__file__).resolve().parents[1] / "alembic/versions/c4e9a2f7b6d1_add_pending_suggestion_owner.py"
        spec = importlib.util.spec_from_file_location("pending_suggestion_owner_migration", source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_migration_adds_owner_and_backfills_default_user(self):
        module = self._module()
        self.assertEqual(module.down_revision, "b8f3d6a1c9e4")
        engine = sa.create_engine("sqlite:///:memory:")
        try:
            with engine.begin() as conn:
                conn.execute(sa.text("CREATE TABLE users (id INTEGER PRIMARY KEY, role VARCHAR(20))"))
                conn.execute(sa.text("INSERT INTO users VALUES (1, 'user'), (2, 'admin'), (3, 'admin')"))
                conn.execute(sa.text(
                    "CREATE TABLE pending_suggestions (id INTEGER PRIMARY KEY, source_id VARCHAR(255))"))
                conn.execute(sa.text("INSERT INTO pending_suggestions (source_id) VALUES ('old')"))

            def apply(fn):
                with engine.begin() as conn:
                    original = module.op
                    module.op = Operations(MigrationContext.configure(conn))
                    try:
                        fn()
                    finally:
                        module.op = original

            apply(module.upgrade)
            with engine.connect() as conn:
                self.assertEqual(conn.execute(sa.text("SELECT user_id FROM pending_suggestions")).scalar(), 2)
            self.assertIn(module.INDEX, {i["name"] for i in sa.inspect(engine).get_indexes("pending_suggestions")})
            apply(module.downgrade)
            self.assertNotIn("user_id", {c["name"] for c in sa.inspect(engine).get_columns("pending_suggestions")})
            with engine.connect() as conn:
                self.assertEqual(conn.execute(sa.text("SELECT source_id FROM pending_suggestions")).scalar(), "old")
        finally:
            engine.dispose()

    def test_full_chain_creates_owner_column_on_fresh_database(self):
        with tempfile.TemporaryDirectory() as directory:
            os.environ["DB_JOURNAL_MODE"] = "DELETE"
            db = DatabaseService(str(Path(directory) / "chain.db"))
            try:
                columns = {c["name"] for c in sa.inspect(db.db_manager.engine).get_columns("pending_suggestions")}
                self.assertIn("user_id", columns)
            finally:
                db.db_manager.close()


class TestProducers(OwnerDbBase):
    def test_shelf_watch_suggestion_is_owned_by_the_watching_user(self):
        svc = ShelfWatchService(
            booklore_client=MagicMock(), database_service=self.db,
            book_mapping_service=MagicMock(), suggestions_service_factory=MagicMock(),
        )
        matches = [{"bridge_key": "abs:1", "audio_title": "T", "audio_source": "ABS"}]
        self.assertTrue(svc._create_pending_suggestion({"title": "T"}, "f.epub", "g1", matches, user_id=self.bob.id))
        self.assertIsNotNone(self.db.get_pending_suggestion("abs:1", user_id=self.bob.id))
        self.assertIsNone(self.db.get_pending_suggestion("abs:1", user_id=self.alice.id))
        # A dismissal by bob suppresses re-suggesting for bob only.
        self.db.dismiss_suggestion("abs:1", user_id=self.bob.id)
        self.assertFalse(svc._create_pending_suggestion({"title": "T"}, "f.epub", "g1", matches, user_id=self.bob.id))
        self.assertTrue(svc._create_pending_suggestion({"title": "T"}, "f.epub", "g1", matches, user_id=self.alice.id))


class TestScanScoping(OwnerDbBase):
    def _service(self):
        svc = SuggestionsService(
            database_service=self.db, container=MagicMock(), manager=MagicMock(),
            get_audiobooks_conditionally=lambda: [{
                "audio_source": "ABS", "audio_source_id": "abs-1",
                "audio_title": "Shared", "audio_author": "X",
            }],
            get_searchable_ebooks=lambda _q: [],
            audiobook_matches_search=lambda _ab, _q: False,
            get_abs_author=lambda _ab: "",
            logger=MagicMock(),
        )
        return svc

    def _scan_as(self, uid):
        token = set_current_user_id(uid)
        try:
            return self._service().scan_library_suggestions()
        finally:
            reset_current_user_id(token)

    def test_scan_does_not_hide_a_suggestion_because_another_reader_mapped_it(self):
        self.db.save_book(Book(abs_id="abs-1", abs_title="Shared", status="active"))
        self.db.link_user_book(self.alice.id, "abs-1")
        self.assertEqual(self._scan_as(self.alice.id)["stats"]["total_unmatched"], 0)
        self.assertEqual(self._scan_as(self.bob.id)["stats"]["total_unmatched"], 1)

    def test_scan_ignored_filter_is_per_reader(self):
        self.save(self.alice.id, "ABS:abs-1", status="ignored")
        with patch.object(SuggestionsService, "_audio_bridge_key", return_value="ABS:abs-1"):
            self.assertEqual(self._scan_as(self.alice.id)["stats"]["total_unmatched"], 0)
            self.assertEqual(self._scan_as(self.bob.id)["stats"]["total_unmatched"], 1)


class TestWebReaders(OwnerDbBase):
    def setUp(self):
        super().setUp()
        import src.web_server as web
        self.web = web
        self.app = Flask(__name__)
        self.app.secret_key = "t"
        self.app.config["LOGIN_DISABLED"] = True
        self.save(self.alice.id, "mine", title="Alice's")
        self.save(self.bob.id, "theirs", title="Bob's")
        self.save(self.bob.id, "shared-key", title="Bob shared", origin="shelf_watch")

    def test_json_routes_read_and_write_only_the_acting_users_rows(self):
        with self.app.test_request_context(), \
                patch.object(self.web, "database_service", self.db), \
                patch.object(self.web, "get_current_user_id", return_value=self.alice.id):
            ids = {r["source_id"] for r in self.web.get_suggestions().get_json()}
            self.assertEqual(ids, {"mine"})
            _resp, status = self.web.dismiss_suggestion("theirs")
            self.assertEqual(status, 404)
            self.assertTrue(self.web.ignore_suggestion("mine").get_json()["success"])
        self.assertIsNotNone(self.db.get_pending_suggestion("theirs", user_id=self.bob.id))

    def test_watch_review_lookup_ignores_other_readers_rows(self):
        item = {"bridge_key": "shared-key"}
        with self.app.test_request_context(), \
                patch.object(self.web, "database_service", self.db):
            with patch.object(self.web, "get_current_user_id", return_value=self.bob.id):
                self.assertIsNotNone(self.web._queue_item_shelf_watch_metadata(item))
            with patch.object(self.web, "get_current_user_id", return_value=self.alice.id):
                self.assertIsNone(self.web._queue_item_shelf_watch_metadata(item))
                self.assertIsNone(self.web._watch_suggestion_review("shared-key"))


if __name__ == "__main__":
    unittest.main()
