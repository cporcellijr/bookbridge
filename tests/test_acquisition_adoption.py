"""Get the missing half (#585) Phase 5: adopt the reader's own BookOrbit audiobook requests and pair them with their ebook."""

import importlib.util
import os
import shutil
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from src.api.bookorbit_client import BookOrbitClient
from src.db.database_service import DatabaseService
from src.sync_manager import SyncManager
from src.utils.config_loader import ALL_SETTINGS, DEFAULT_CONFIG
from src.utils.time_utils import utcnow
from src.utils.user_config import PER_USER_CREDENTIAL_KEYS, PER_USER_FIELD_GROUPS
from tests.test_acquisition_bookorbit import _response
from tests.test_acquisition_linking import _EPUB, _LOG, _M4B, LinkingBase

KEY = "BOOKORBIT_ADOPT_REQUESTS"
_MIGRATION = Path(__file__).resolve().parents[1] / "alembic/versions/b5d2f8a4c7e1_acquisition_adopted.py"
_PAIRED = "That audiobook is already paired with another ebook."


def _stamp(days_ago: float = 1) -> str:
    return (utcnow() - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _item(request_id, user_id=5, days_ago=1, **extra):
    base = {
        "id": request_id, "userId": user_id, "mediaKind": "audiobook", "status": "available",
        "title": "Example Book", "authors": ["A. Writer"], "seriesName": None, "seriesIndex": None,
        "matchedBookId": 6099, "updatedAt": _stamp(days_ago), "createdAt": _stamp(days_ago + 1),
    }
    base.update(extra)
    return base


def _ebook(book_id=2167, **extra):
    base = {"id": book_id, "title": "Example Book", "authors": "A. Writer", "seriesIndex": None}
    base.update(extra)
    return base


class AdoptionBase(LinkingBase):
    def setUp(self):
        super().setUp()
        patcher = patch.dict(os.environ, {"ACQUISITION_ALLOW_READERS": "true", KEY: "false"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.bo.current_user_id.return_value = 5
        self.pages = {}
        self.bo.list_requests.side_effect = self._list
        self.bo.get_request.return_value = {"status": "available", "matchedBookId": 6099}
        self.details = {6099: _M4B}
        self.bo.get_book_detail.side_effect = lambda book_id, force=False, **_kw: self.details.get(book_id) or (
            {"files": [{"format": "epub", "filename": f"{book_id}.epub", "role": "primary"}]})
        self.details[2167] = _EPUB
        self.bo.get_all_ebooks.return_value = [_ebook()]
        self.opt_in(self.user, "true")

    def _list(self, status=None, media_kind=None, page=1, limit=50):
        return self.pages.get(page, {"items": [], "total": 0})

    def opt_in(self, user, value):
        self.db.set_user_credential(user.id, KEY, value)

    def serve(self, *items):
        """Serve the items as 100-per-page results, the way BookOrbit pages them."""
        self.pages = {
            n + 1: {"items": list(items[n * 100:(n + 1) * 100]), "total": len(items)}
            for n in range((len(items) + 99) // 100)
        }

    def rows(self, user=None):
        return self.db.get_acquisition_requests((user or self.user).id)

    def review_scanner(self, *ebooks):
        """Make Discover's scan find the arrived audiobook for each of these candidate ebooks."""
        self.bo.search_ebooks.return_value = [
            {"id": e["id"], "title": e["title"], "authors": e["authors"], "fileName": f"{e['id']}.epub"} for e in ebooks]
        scanner = MagicMock()
        scanner._build_audiobook_candidate_pool.return_value = [{"bridge_key": "bookorbit:6099"}]
        scanner._scan_single_ebook.return_value = {"matches": [
            {"audio_source": "BookOrbit", "audio_source_id": "6099", "bridge_key": "bookorbit:6099",
             "audio_title": "Example Book", "audio_author": "A. Writer", "score": 80}]}
        self.shelf.set_suggestions_service_factory(lambda: scanner)

    def assert_no_pairing(self, status="needs_look"):
        [row] = self.rows()
        self.assertEqual(row["status"], status)
        self.assertIsNone(row["linked_abs_id"])
        self.assertIsNone(self.db.get_book("bookorbit:6099"))
        return row


class TestOptIn(AdoptionBase):
    def test_off_makes_no_calls_and_adds_no_rows(self):
        self.opt_in(self.user, "false")
        self.serve(_item(31))
        self.assertFalse(self.service.has_work(self.user.id))
        self.refresh()
        self.bo.current_user_id.assert_not_called()
        self.bo.list_requests.assert_not_called()
        self.assertEqual(self.rows(), [])

    def test_on_in_either_spelling_adopts(self):
        for n, value in enumerate(("true", "on")):
            with self.subTest(value=value):
                self.service._adoption_scanned.clear()
                self.opt_in(self.user, value)
                self.serve(_item(40 + n, seriesName="Saga", seriesIndex=2, authors=["A. Writer", "B. Other"]))
                self.bo.get_request.return_value = {"status": "approved"}
                self.assertTrue(self.service.has_work(self.user.id))
                self.refresh()
                row = self.db.find_acquisition_request(self.user.id, "BookOrbit", str(40 + n))
                self.assertEqual(
                    (row["adopted"], row["status"], row["media_kind"], row["remote_kind"], row["authors"],
                     row["series_name"], row["series_index"], row["partner_source_id"]),
                    (True, "in_progress", "audiobook", "request", "A. Writer, B. Other", "Saga", 2, None))

    def test_regular_user_does_not_inherit_the_admins_opt_in(self):
        self.opt_in(self.user, "")
        os.environ[KEY] = "true"
        self.serve(_item(31))
        self.assertFalse(self.service.has_work(self.user.id))
        self.refresh()
        self.bo.list_requests.assert_not_called()
        self.assertEqual(self.rows(), [])

    def test_non_admin_is_refused_while_readers_may_not_request(self):
        os.environ["ACQUISITION_ALLOW_READERS"] = "false"
        self.serve(_item(31))
        self.refresh()
        self.bo.list_requests.assert_not_called()
        self.assertEqual(self.rows(), [])

    def test_admin_adopts_without_the_readers_gate(self):
        os.environ["ACQUISITION_ALLOW_READERS"] = "false"
        self.opt_in(self.admin, "true")
        self.bo.current_user_id.return_value = 1
        self.serve(_item(31, user_id=1))
        self.service.refresh_user(self.admin.id, self.bo)
        self.assertEqual([r["remote_id"] for r in self.rows(self.admin)], ["31"])


class TestScan(AdoptionBase):
    def test_only_the_readers_own_requests_are_adopted(self):
        self.opt_in(self.admin, "true")
        self.bo.current_user_id.return_value = 1
        self.serve(_item(31, user_id=1), _item(32, user_id=2), _item(33, user_id=2))
        self.service.refresh_user(self.admin.id, self.bo)
        self.assertEqual({r["remote_id"] for r in self.rows(self.admin)}, {"31"})
        self.assertEqual(self.rows(), [])

    def test_unidentified_account_skips_the_scan(self):
        self.bo.current_user_id.return_value = None
        self.serve(_item(31))
        self.refresh()
        self.bo.list_requests.assert_not_called()
        self.assertEqual(self.rows(), [])

    def test_pages_are_followed_until_the_total_is_reached(self):
        self.serve(*[_item(1000 + n) for n in range(250)])
        self.service._adopt_requests(self.user.id, self.bo)
        self.assertEqual([c.kwargs["page"] for c in self.bo.list_requests.call_args_list], [1, 2, 3])
        self.assertEqual(len(self.rows()), 250)
        self.assertEqual(self.bo.list_requests.call_args.kwargs["status"], "available")
        self.assertEqual(self.bo.list_requests.call_args.kwargs["media_kind"], "audiobook")

    def test_scan_stops_at_twenty_pages(self):
        old = _item(1, days_ago=90)
        self.bo.list_requests.side_effect = lambda **kw: {"items": [old], "total": 99999}
        self.service._adopt_requests(self.user.id, self.bo)
        self.assertEqual(self.bo.list_requests.call_count, 20)

    def test_two_scans_add_no_duplicate_row_and_pair_once(self):
        self.serve(_item(31))
        self.refresh()
        self.service._adoption_scanned.clear()
        self.bo.list_requests.reset_mock()
        self.refresh()
        self.bo.list_requests.assert_called()
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual([b.abs_id for b in self.db.get_all_books()], ["bookorbit:6099"])

    def test_dismissed_row_is_not_adopted_again(self):
        self.add_row(remote_id="31", dismissed=True)
        self.serve(_item(31))
        self.refresh()
        self.assertEqual(self.rows(), [])
        self.assertFalse(self.db.find_acquisition_request(self.user.id, "BookOrbit", "31", include_dismissed=True)["adopted"])

    def test_row_the_bridge_made_keeps_its_partner(self):
        made = self.add_row(remote_id="31", status="paired", linked_abs_id="bookorbit:6099")
        self.serve(_item(31))
        self.refresh()
        [row] = self.rows()
        self.assertEqual((row["id"], row["adopted"], row["partner_source_id"], row["linked_abs_id"]),
                         (made["id"], False, "2167", "bookorbit:6099"))

    def test_requests_older_than_thirty_days_are_history(self):
        self.serve(_item(31, days_ago=31), _item(32, days_ago=29))
        self.refresh()
        self.assertEqual([r["remote_id"] for r in self.rows()], ["32"])

    def test_scan_runs_at_most_every_ten_minutes(self):
        self.serve(_item(31))
        self.bo.get_request.return_value = {"status": "approved"}
        self.refresh()
        self.assertEqual(self.bo.list_requests.call_count, 1)
        self.service.refresh_user(self.user.id, self.bo)
        self.assertEqual(self.bo.list_requests.call_count, 1)
        self.service._adoption_scanned[self.user.id] = utcnow() - timedelta(minutes=11)
        self.service.refresh_user(self.user.id, self.bo)
        self.assertEqual(self.bo.list_requests.call_count, 2)


class TestPairing(AdoptionBase):
    def test_unique_match_pairs_with_the_exact_ids(self):
        self.serve(_item(31))
        with self.assertLogs(_LOG, level="INFO") as logs:
            self.refresh()
        [row] = self.rows()
        self.assertEqual((row["status"], row["linked_abs_id"], row["adopted"]), ("paired", "bookorbit:6099", True))
        self.assertEqual((row["partner_source"], row["partner_source_id"], row["partner_filename"]),
                         ("BookOrbit", "2167", "Example Book.epub"))
        book = self.db.get_book("bookorbit:6099")
        self.assertEqual((book.audio_source, book.audio_source_id, book.ebook_source, book.ebook_source_id,
                          book.ebook_filename), ("BookOrbit", "6099", "BookOrbit", "2167", "Example Book.epub"))
        lines = "\n".join(logs.output)
        self.assertIn(f"user {self.user.id} adopted 1 BookOrbit request(s) made in BookOrbit", lines)
        self.assertIn(f"user {self.user.id} adopted request {row['id']} matched ebook 2167 (title, authors, series agree)", lines)

    def test_series_index_missing_on_one_side_still_pairs(self):
        self.serve(_item(31, seriesName="Saga", seriesIndex=2))
        self.refresh()
        self.assertEqual(self.rows()[0]["status"], "paired")

    def test_two_eligible_ebooks_go_to_review_not_pairing(self):
        twins = [_ebook(2167), _ebook(2168)]
        self.bo.get_all_ebooks.return_value = twins
        self.review_scanner(*twins)
        self.serve(_item(31))
        with self.assertLogs(_LOG, level="INFO") as logs:
            self.refresh()
        row = self.assert_no_pairing()
        self.assertIsNone(row["partner_source_id"])
        self.assertIsNotNone(self.db.get_pending_suggestion("bookorbit:6099", user_id=self.user.id))
        self.assertTrue(any("has no unique ebook match (2 eligible of 2 ebooks" in line and "left for review" in line
                            for line in logs.output))

    def test_series_conflict_excludes_the_only_title_match(self):
        self.bo.get_all_ebooks.return_value = [_ebook(seriesIndex=1)]
        self.review_scanner(_ebook())
        self.serve(_item(31, seriesName="Saga", seriesIndex=2))
        self.refresh()
        self.assert_no_pairing()
        self.assertIsNotNone(self.db.get_pending_suggestion("bookorbit:6099", user_id=self.user.id))

    def test_ebook_already_mapped_with_audio_is_excluded(self):
        self.save_mapping(
            "abs-1", sync_mode="audiobook", audio_source="ABS", audio_source_id="abs-1",
            ebook_filename="Example Book.epub", ebook_source="BookOrbit", ebook_source_id="2167")
        self.review_scanner(_ebook())
        self.serve(_item(31))
        self.refresh()
        self.assert_no_pairing()
        self.assertEqual(self.db.get_book("abs-1").audio_source_id, "abs-1")

    def test_request_without_authors_is_never_auto_paired(self):
        self.review_scanner(_ebook())
        self.serve(_item(31, authors=[]))
        self.refresh()
        self.assert_no_pairing()

    def test_nothing_to_offer_leaves_the_row_arrived_without_a_mapping(self):
        self.bo.get_all_ebooks.return_value = []
        self.bo.search_ebooks.return_value = []
        self.serve(_item(31))
        self.refresh()
        row = self.assert_no_pairing(status="arrived")
        self.assertIn("No single matching ebook", row["status_detail"])

    def test_audiobook_in_a_complete_mapping_is_dismissed_quietly(self):
        self.save_mapping(
            "bookorbit:6099", sync_mode="audiobook", audio_source="BookOrbit", audio_source_id="6099",
            ebook_filename="Other.epub", ebook_source="BookOrbit", ebook_source_id="9")
        self.serve(_item(31))
        with self.assertLogs(_LOG, level="INFO") as logs:
            self.refresh()
        self.assertEqual(self.rows(), [])
        row = self.db.find_acquisition_request(self.user.id, "BookOrbit", "31", include_dismissed=True)
        self.assertEqual((row["status"], row["status_detail"], row["dismissed"]), ("arrived", _PAIRED, True))
        self.assertEqual([(b.abs_id, b.ebook_source_id) for b in self.db.get_all_books()], [("bookorbit:6099", "9")])
        self.assertEqual(sum(_PAIRED in line for line in logs.output), 1)

    def test_review_approval_completes_an_adopted_row(self):
        twins = [_ebook(2167), _ebook(2168)]
        self.bo.get_all_ebooks.return_value = twins
        self.review_scanner(*twins)
        self.serve(_item(31))
        self.refresh()
        [row] = self.rows()
        self.save_mapping(
            "bookorbit:6099", sync_mode="audiobook", audio_source="BookOrbit", audio_source_id="6099",
            ebook_filename="2168.epub", ebook_source="BookOrbit", ebook_source_id="2168")
        self.age(row["id"])
        self.refresh()
        self.assertEqual(self.rows()[0]["status"], "paired")

    def test_disabling_stops_new_adoption_but_keeps_following_and_keeps_data(self):
        self.serve(_item(31))
        self.refresh()
        self.assertEqual(self.rows()[0]["status"], "paired")
        open_row = self.add_row(remote_id="32", partner_source=None, partner_source_id=None, partner_filename=None,
                                adopted=True)
        self.opt_in(self.user, "false")
        self.service._adoption_scanned.clear()
        self.bo.list_requests.reset_mock()
        self.bo.get_request.reset_mock()
        self.bo.get_request.return_value = {"status": "approved"}
        self.serve(_item(33))
        self.refresh()
        self.bo.list_requests.assert_not_called()
        self.assertEqual({r["remote_id"] for r in self.rows()}, {"31", "32"})
        self.bo.get_request.assert_called_once_with("32")
        self.assertEqual(self.db.get_acquisition_request(self.user.id, open_row["id"])["remote_status"], "approved")
        self.assertIsNotNone(self.db.get_book("bookorbit:6099"))


class TestCycle(AdoptionBase):
    def manager(self, registry):
        manager = SyncManager.__new__(SyncManager)
        manager.acquisition_service = self.service
        manager.user_client_registry = registry
        return manager

    def test_opted_in_user_with_no_open_rows_is_scanned(self):
        self.serve(_item(31))
        registry = MagicMock()
        registry.get_clients.return_value = MagicMock(bookorbit_client=self.bo, shelfmark_client=None)
        self.manager(registry)._refresh_acquisition_requests(self.user.id)
        registry.get_clients.assert_called_once_with(self.user.id)
        self.bo.list_requests.assert_called_once()
        self.assertEqual(self.rows()[0]["status"], "paired")

    def test_user_without_the_opt_in_makes_no_http_call(self):
        self.opt_in(self.user, "false")
        self.serve(_item(31))
        registry = MagicMock()
        self.manager(registry)._refresh_acquisition_requests(self.user.id)
        registry.get_clients.assert_not_called()
        self.bo.current_user_id.assert_not_called()
        self.bo.list_requests.assert_not_called()


class TestCurrentUserId(unittest.TestCase):
    def setUp(self):
        self.client = BookOrbitClient(credentials={
            "BOOKORBIT_SERVER": "http://bo.test", "BOOKORBIT_API_KEY": "key-a", "BOOKORBIT_ENABLED": "true"})
        patcher = patch.object(self.client, "_make_request", return_value=_response(200, {"id": 5, "username": "me"}))
        self.request = patcher.start()
        self.addCleanup(patcher.stop)

    def test_reads_the_id_once_per_login(self):
        self.assertEqual(self.client.current_user_id(), 5)
        self.assertEqual(self.client.current_user_id(), 5)
        self.request.assert_called_once_with("GET", "/api/v1/auth/me", None)

    def test_a_changed_login_reads_again(self):
        self.client.current_user_id()
        self.client._creds["BOOKORBIT_API_KEY"] = "key-b"
        self.request.return_value = _response(200, {"id": 8})
        self.assertEqual(self.client.current_user_id(), 8)
        self.assertEqual(self.request.call_count, 2)

    def test_failure_answers_none_and_is_not_cached(self):
        self.request.return_value = _response(500)
        self.assertIsNone(self.client.current_user_id())
        self.request.return_value = _response(200, {"id": 5})
        self.assertEqual(self.client.current_user_id(), 5)
        self.request.return_value = _response(200, {"id": "5"})
        self.client._creds["BOOKORBIT_API_KEY"] = "key-c"
        self.assertIsNone(self.client.current_user_id())


class TestSettingAndMigration(unittest.TestCase):
    def test_setting_is_registered_per_user_with_a_false_default(self):
        self.assertIn(KEY, ALL_SETTINGS)
        self.assertEqual(DEFAULT_CONFIG[KEY], "false")
        self.assertIn(KEY, PER_USER_CREDENTIAL_KEYS)
        fields = {k: t for k, _label, t in dict(PER_USER_FIELD_GROUPS)["BookOrbit"]}
        self.assertEqual(fields[KEY], "bool")

    def _load(self):
        spec = importlib.util.spec_from_file_location("adopted_migration", _MIGRATION)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_migration_adds_a_not_null_false_column_idempotently_and_chains_to_the_previous_head(self):
        module = self._load()
        self.assertEqual(module.down_revision, "a7e3c9d1b5f2")
        engine = sa.create_engine("sqlite:///:memory:")
        try:
            with engine.begin() as conn:
                conn.execute(sa.text("CREATE TABLE acquisition_requests (id INTEGER PRIMARY KEY)"))
                conn.execute(sa.text("INSERT INTO acquisition_requests (id) VALUES (1)"))
            for _ in range(2):
                with engine.begin() as conn:
                    module.op = Operations(MigrationContext.configure(conn))
                    module.upgrade()
            columns = {c["name"]: c for c in sa.inspect(engine).get_columns("acquisition_requests")}
            self.assertFalse(columns["adopted"]["nullable"])
            with engine.connect() as conn:
                self.assertEqual(conn.execute(sa.text("SELECT adopted FROM acquisition_requests")).scalar(), 0)
            with engine.begin() as conn:
                module.op = Operations(MigrationContext.configure(conn))
                module.downgrade()
            self.assertEqual({c["name"] for c in sa.inspect(engine).get_columns("acquisition_requests")}, {"id"})
        finally:
            engine.dispose()

    def test_fresh_database_reaches_head_with_the_column_and_a_single_head(self):
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        tmp = tempfile.mkdtemp()
        svc = DatabaseService(os.path.join(tmp, "fresh.db"))
        try:
            columns = {c["name"] for c in sa.inspect(svc.db_manager.engine).get_columns("acquisition_requests")}
            self.assertIn("adopted", columns)
        finally:
            svc.db_manager.close()
            shutil.rmtree(tmp, ignore_errors=True)
        root = Path(__file__).resolve().parents[1]
        cfg = Config(str(root / "alembic.ini"))
        cfg.set_main_option("script_location", str(root / "alembic"))
        self.assertEqual(ScriptDirectory.from_config(cfg).get_heads(), ["b5d2f8a4c7e1"])


if __name__ == "__main__":
    unittest.main()
