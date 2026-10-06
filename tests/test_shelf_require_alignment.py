import ast
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import src.web_server as web_server
from src.api.booklore_client import BookloreClient
from src.db.database_service import DatabaseService
from src.db.models import Book, BookAlignment
from src.services.forge_service import ForgeService
from src.services.shelf_watch_service import ShelfWatchService
from src.utils.logging_utils import get_persistent_condition_logger

READER = 7


def _grimmory_client():
    client = MagicMock()
    client.is_configured.return_value = True
    client.add_book_id_to_shelf.return_value = True
    client.add_to_shelf.return_value = True
    client.remove_from_shelf.return_value = True
    client.move_between_shelves.return_value = True
    return client


class Shelving:
    """web_server wired to a real database, a global Grimmory client and per-user bundles."""

    def __init__(self, db, monkeypatch):
        self.db = db
        self.monkeypatch = monkeypatch
        self.global_client = _grimmory_client()
        self.bundles = {}
        container = MagicMock()
        container.user_client_registry.return_value.get_clients.side_effect = lambda uid: self.bundles[uid]
        monkeypatch.setattr(web_server, "database_service", db)
        monkeypatch.setattr(web_server, "container", container)
        monkeypatch.setattr(web_server, "_global_clients", SimpleNamespace(booklore_client=self.global_client))
        monkeypatch.setattr(web_server, "uc", lambda: MagicMock(booklore_client=self.global_client))
        monkeypatch.setenv("BOOKLORE_SHELF_NAME", "ABS Synced")
        monkeypatch.setenv("BOOKLORE_SHELF_WATCH_ENABLED", "true")
        monkeypatch.setenv("BOOKLORE_SHELF_WATCH_NAME", "Up Next")
        monkeypatch.delenv("BOOKLORE_SHELF_OWNER", raising=False)

    def user(self, username, shelf_name=None, active=True):
        user = self.db.create_user(username)
        self.db.set_user_active(user.id, active)
        credentials = {"__allow_global_fallback__": False}
        if shelf_name:
            credentials["BOOKLORE_SHELF_NAME"] = shelf_name
        client = _grimmory_client()
        self.bundles[user.id] = MagicMock(booklore_client=client, credentials=credentials)
        return user.id, client

    def acting_as(self, user_id):
        self.monkeypatch.setattr(web_server, "get_current_user_id", lambda: user_id)
        self.monkeypatch.setattr(web_server, "uc", lambda: self.bundles[user_id])

    def owner(self, active=True):
        self.monkeypatch.setenv("BOOKLORE_SHELF_OWNER", "service-account")
        return self.user("service-account", active=active)

    def book(self, abs_id="a1", source="BookLore", source_id="45", sync_mode="audiobook",
             align_method=None, user_id=None, status="active"):
        book = self.db.save_book(Book(
            abs_id=abs_id, abs_title=f"Title {abs_id}", ebook_filename=f"{abs_id}.epub",
            ebook_source=source, ebook_source_id=source_id, sync_mode=sync_mode, user_id=user_id,
            status=status,
        ))
        if align_method:
            with self.db.get_session() as session:
                session.add(BookAlignment(abs_id=abs_id, alignment_map_json="[]", align_method=align_method))
        return book

    def cleanup(self, book, tmp_path):
        self.monkeypatch.setattr(web_server, "DATA_DIR", tmp_path, raising=False)
        self.monkeypatch.setattr(web_server, "manager", None)
        web_server.container.epub_cache_dir.return_value = str(tmp_path)
        web_server.cleanup_mapping_resources(book)


@pytest.fixture(autouse=True)
def _reset_persistent_conditions():
    get_persistent_condition_logger().reset()
    yield
    get_persistent_condition_logger().reset()


@pytest.fixture
def db(tmp_path):
    service = DatabaseService(str(tmp_path / "database.db"))
    yield service
    service.db_manager.close()


@pytest.fixture
def shelving(db, monkeypatch):
    monkeypatch.setenv("BOOKLORE_SHELF_REQUIRE_ALIGNMENT", "false")
    return Shelving(db, monkeypatch)


@pytest.fixture(params=["true", "on"])
def required(request, shelving, monkeypatch):
    monkeypatch.setenv("BOOKLORE_SHELF_REQUIRE_ALIGNMENT", request.param)
    return shelving


class TestPendingShelfAdds:
    def test_records_each_reader_once(self, db):
        db.add_pending_shelf_add("a1", READER)
        db.add_pending_shelf_add("a1", READER)
        db.add_pending_shelf_add("a1", None)
        assert db.get_pending_shelf_adds() == [("a1", READER), ("a1", None)]

    def test_survives_a_restart(self, db, tmp_path):
        db.add_pending_shelf_add("a1", READER)
        reopened = DatabaseService(str(tmp_path / "database.db"))
        try:
            assert reopened.get_pending_shelf_adds() == [("a1", READER)]
        finally:
            reopened.db_manager.close()

    def test_removes_one_reader_or_all(self, db):
        for user_id in (READER, 8, None):
            db.add_pending_shelf_add("a1", user_id)
        db.add_pending_shelf_add("a2", READER)

        db.remove_pending_shelf_adds("a1", READER)
        assert db.get_pending_shelf_adds() == [("a1", 8), ("a1", None), ("a2", READER)]

        db.remove_pending_shelf_adds("a1")
        assert db.get_pending_shelf_adds() == [("a1", 8), ("a2", READER)]

        db.remove_pending_shelf_adds("a1", all_users=True)
        assert db.get_pending_shelf_adds() == [("a2", READER)]

    def test_successful_assignment_survives_a_restart(self, db, tmp_path):
        db.record_shelf_assignment("a1", READER, 12, "Shared", "grimmory-45")
        reopened = DatabaseService(str(tmp_path / "database.db"))
        try:
            assert reopened.get_shelf_assignments("a1") == [{
                "abs_id": "a1", "reader_id": READER,
                "target_user_id": 12, "shelf_name": "Shared", "source_id": "grimmory-45",
            }]
        finally:
            reopened.db_manager.close()


class TestMatchTimeShelving:
    def test_aligned_only_defers_the_add_and_still_clears_up_next(self, required):
        book = required.book()
        reader, client = required.user("reader")
        required.acting_as(reader)

        web_server._shelve_matched_ebook("a1.epub", "BookLore", "45", book=book)

        client.add_to_shelf.assert_not_called()
        client.add_book_id_to_shelf.assert_not_called()
        client.remove_from_shelf.assert_called_once_with("a1.epub", "Up Next")
        assert required.db.get_pending_shelf_adds() == [("a1", reader)]

    def test_off_shelves_at_match_time(self, shelving):
        book = shelving.book()

        web_server._shelve_matched_ebook("a1.epub", "BookLore", "45", book=book)

        shelving.global_client.add_to_shelf.assert_called_once_with("a1.epub", "ABS Synced")
        assert shelving.db.get_pending_shelf_adds() == []

    @pytest.mark.parametrize("source, source_id, sync_mode", [
        ("", None, "audiobook"),
        ("BookLore", None, "audiobook"),
        ("BookLore", "45", "ebook_only"),
    ])
    def test_books_the_reconcile_cannot_finish_are_shelved_at_match_time(
            self, required, source, source_id, sync_mode):
        book = required.book(source=source, source_id=source_id, sync_mode=sync_mode)

        web_server._shelve_matched_ebook("a1.epub", source, source_id, book=book)

        required.global_client.add_to_shelf.assert_called_once_with("a1.epub", "ABS Synced")
        assert required.db.get_pending_shelf_adds() == []

    def test_every_match_path_hands_its_book_to_the_decision(self):
        tree = ast.parse(Path(web_server.__file__).read_text(encoding="utf-8"))
        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_shelve_matched_ebook"
        ]
        assert calls
        assert all(any(keyword.arg == "book" for keyword in call.keywords) for call in calls)


class TestOtherShelvingPathsDefer:
    def _watcher(self, shelving, saved):
        mapping = MagicMock()
        mapping.create_audio_mapping_from_match.return_value = saved
        mapping.create_ebook_only_mapping.return_value = saved
        return ShelfWatchService(
            booklore_client=shelving.global_client,
            database_service=shelving.db,
            book_mapping_service=mapping,
        )

    def test_shelf_watch_auto_match_only_leaves_up_next(self, required):
        watcher = self._watcher(required, required.book())
        client = _grimmory_client()

        watcher._create_audio_mapping_and_move(
            {"title": "Title"}, "a1.epub", "45", {"audio_source": "ABS", "audio_source_id": "a1"},
            "Up Next", "ABS Synced", user_id=READER, active_client=client,
        )

        client.move_between_shelves.assert_not_called()
        client.remove_from_shelf.assert_called_once_with("a1.epub", "Up Next")
        assert required.db.get_pending_shelf_adds() == [("a1", READER)]

    def test_shelf_watch_ebook_only_still_moves(self, required):
        watcher = self._watcher(required, required.book(sync_mode="ebook_only"))
        client = _grimmory_client()

        watcher._create_ebook_only_and_move(
            {"title": "Title"}, "a1.epub", "45", "Up Next", "ABS Synced", active_client=client,
        )

        client.move_between_shelves.assert_called_once_with("a1.epub", "Up Next", "ABS Synced")
        assert required.db.get_pending_shelf_adds() == []

    def test_shelf_watch_moves_when_off(self, shelving):
        watcher = self._watcher(shelving, shelving.book())
        client = _grimmory_client()

        watcher._create_audio_mapping_and_move(
            {"title": "Title"}, "a1.epub", "45", {"audio_source": "ABS", "audio_source_id": "a1"},
            "Up Next", "ABS Synced", active_client=client,
        )

        client.move_between_shelves.assert_called_once_with("a1.epub", "Up Next", "ABS Synced")

    def test_shelf_watch_approval_only_leaves_up_next(self, required, monkeypatch):
        book = required.book()
        reader, client = required.user("reader")
        required.acting_as(reader)
        monkeypatch.setattr(web_server, "_shelf_watch_clients_for", lambda meta: (client, "Up Next", "ABS Synced"))

        assert web_server._complete_shelf_watch_approval({"grimmory_filename": "a1.epub"}, book=book)

        client.move_between_shelves.assert_not_called()
        client.remove_from_shelf.assert_called_once_with("a1.epub", "Up Next")
        assert required.db.get_pending_shelf_adds() == [("a1", reader)]

    def test_shelf_watch_approval_moves_when_off(self, shelving, monkeypatch):
        book = shelving.book()
        client = shelving.global_client
        monkeypatch.setattr(web_server, "_shelf_watch_clients_for", lambda meta: (client, "Up Next", "ABS Synced"))

        assert web_server._complete_shelf_watch_approval({"grimmory_filename": "a1.epub"}, book=book)

        client.move_between_shelves.assert_called_once_with("a1.epub", "Up Next", "ABS Synced")

    def _forge(self, shelving, client):
        return ForgeService(
            database_service=shelving.db, abs_client=MagicMock(), booklore_client=client,
            storyteller_client=MagicMock(), library_service=MagicMock(), ebook_parser=MagicMock(),
            transcriber=MagicMock(), alignment_service=MagicMock(), bookorbit_client=MagicMock(),
        )

    def test_auto_forge_defers_for_the_books_owner(self, required):
        reader, client = required.user("reader")
        book = required.book(user_id=reader)

        self._forge(required, client)._shelve_forged_ebook(book, "a1.epub")

        client.add_to_shelf.assert_not_called()
        assert required.db.get_pending_shelf_adds() == [("a1", reader)]

    def test_auto_forge_shelves_when_off(self, shelving):
        reader, client = shelving.user("reader")
        book = shelving.book(user_id=reader)

        self._forge(shelving, client)._shelve_forged_ebook(book, "a1.epub")

        client.add_to_shelf.assert_called_once_with("a1.epub")

    def _auto_match(self, shelving, book, reader):
        shelving.acting_as(reader)
        shelving.monkeypatch.setenv("SUGGESTIONS_AUTO_MATCH_ENABLED", "true")
        web_server.container.book_mapping_service.return_value.create_audio_mapping_from_match.return_value = book
        suggestion = {
            "abs_id": "a1", "abs_title": "Title a1", "audio_source": "ABS", "audio_source_id": "a1",
            "matches": [{"ebook_filename": "a1.epub", "source": "BookLore", "source_id": "45", "score": 100.0}],
        }
        return web_server._auto_match_suggestions({"suggestions": [suggestion], "stats": {}}, user_id=reader)

    def test_suggestions_auto_match_defers_for_the_scanning_reader(self, required):
        reader, client = required.user("reader")

        results = self._auto_match(required, required.book(), reader)

        assert results["stats"]["auto_matched"] == 1
        client.add_to_shelf.assert_not_called()
        assert required.db.get_pending_shelf_adds() == [("a1", reader)]

    def test_suggestions_auto_match_shelves_when_off(self, shelving):
        reader, client = shelving.user("reader", shelf_name="Kobo")

        self._auto_match(shelving, shelving.book(), reader)

        client.add_to_shelf.assert_called_once()
        assert client.add_to_shelf.call_args.args[0] == "a1.epub"
        assert shelving.db.get_pending_shelf_adds() == []


class TestReconcile:
    def test_nothing_pending_never_touches_grimmory(self, required):
        required.book(align_method="lexical")

        assert web_server._reconcile_aligned_shelf() is None

        required.global_client.add_book_id_to_shelf.assert_not_called()
        required.global_client.list_books_on_shelf.assert_not_called()

    @pytest.mark.parametrize("align_method", ["ctc", "lexical", "lexical_timed"])
    def test_adds_a_precisely_aligned_book(self, required, align_method):
        required.book(align_method=align_method)
        required.db.add_pending_shelf_add("a1", None)

        assert web_server._reconcile_aligned_shelf() == 1

        required.global_client.add_book_id_to_shelf.assert_called_once_with("45", "ABS Synced")
        assert required.db.get_pending_shelf_adds() == []

    @pytest.mark.parametrize("align_method", [None, "linear", "storyteller_linear", "llm_anchor"])
    def test_unaligned_and_coarse_maps_keep_waiting(self, required, align_method):
        required.book(align_method=align_method)
        required.db.add_pending_shelf_add("a1", None)

        assert web_server._reconcile_aligned_shelf() == 0

        required.global_client.add_book_id_to_shelf.assert_not_called()
        assert required.db.get_pending_shelf_adds() == [("a1", None)]

    def test_a_book_still_being_processed_keeps_waiting(self, required):
        required.book(align_method="lexical", status="pending")
        required.db.add_pending_shelf_add("a1", None)

        assert web_server._reconcile_aligned_shelf() == 0

        assert required.db.get_pending_shelf_adds() == [("a1", None)]

    def test_a_book_is_added_once_so_a_readers_removal_sticks(self, required):
        required.book(align_method="lexical")
        required.db.add_pending_shelf_add("a1", None)
        required.global_client.list_books_on_shelf.return_value = []

        web_server._reconcile_aligned_shelf()
        web_server._reconcile_aligned_shelf()

        required.global_client.add_book_id_to_shelf.assert_called_once_with("45", "ABS Synced")
        required.global_client.list_books_on_shelf.assert_not_called()

    def test_a_failed_add_stays_pending_without_touching_other_books(self, required):
        required.book("a1", source_id="11", align_method="lexical")
        required.book("a2", source_id="22", align_method="lexical")
        required.db.add_pending_shelf_add("a2", None)
        required.global_client.add_book_id_to_shelf.return_value = False

        assert web_server._reconcile_aligned_shelf() == 0

        required.global_client.add_book_id_to_shelf.assert_called_once_with("22", "ABS Synced")
        assert required.db.get_pending_shelf_adds() == [("a2", None)]

    @pytest.mark.parametrize("off", ["false", "off"])
    def test_turning_the_option_off_shelves_what_was_waiting(self, required, monkeypatch, off):
        required.book()
        required.db.add_pending_shelf_add("a1", None)
        monkeypatch.setenv("BOOKLORE_SHELF_REQUIRE_ALIGNMENT", off)

        assert web_server._reconcile_aligned_shelf() == 1

        required.global_client.add_book_id_to_shelf.assert_called_once_with("45", "ABS Synced")
        assert required.db.get_pending_shelf_adds() == []

    def test_each_reader_gets_their_own_login_and_shelf(self, required):
        required.book(align_method="lexical")
        named, named_client = required.user("named", shelf_name="Reading")
        unnamed, unnamed_client = required.user("unnamed")
        required.db.add_pending_shelf_add("a1", named)
        required.db.add_pending_shelf_add("a1", unnamed)

        assert web_server._reconcile_aligned_shelf() == 2

        named_client.add_book_id_to_shelf.assert_called_once_with("45", "Reading")
        unnamed_client.add_book_id_to_shelf.assert_called_once_with("45", "Kobo")
        required.global_client.add_book_id_to_shelf.assert_not_called()

    def test_named_owner_adds_every_readers_book_to_the_global_shelf(self, required):
        required.book(align_method="lexical")
        reader, reader_client = required.user("reader", shelf_name="Reading")
        _, owner_client = required.owner()
        required.db.add_pending_shelf_add("a1", reader)

        assert web_server._reconcile_aligned_shelf() == 1

        owner_client.add_book_id_to_shelf.assert_called_once_with("45", "ABS Synced")
        reader_client.add_book_id_to_shelf.assert_not_called()
        required.global_client.add_book_id_to_shelf.assert_not_called()

    def test_inactive_owner_never_falls_back_to_another_login(self, required, caplog):
        required.book(align_method="lexical")
        reader, reader_client = required.user("reader")
        owner, owner_client = required.owner(active=False)
        required.db.add_pending_shelf_add("a1", reader)

        with caplog.at_level(logging.DEBUG, logger=web_server.logger.name):
            web_server._reconcile_aligned_shelf()
            web_server._reconcile_aligned_shelf()
            required.db.set_user_active(owner, True)
            web_server._reconcile_aligned_shelf()

        warnings = [r for r in caplog.records if "is not an active BookBridge user" in r.getMessage()]
        assert [r.levelno for r in warnings] == [logging.WARNING, logging.DEBUG]
        assert any("recovered after 2 occurrences" in r.getMessage() for r in caplog.records)
        reader_client.add_book_id_to_shelf.assert_not_called()
        required.global_client.add_book_id_to_shelf.assert_not_called()
        owner_client.add_book_id_to_shelf.assert_called_once_with("45", "ABS Synced")

    def test_a_failure_every_cycle_warns_once_then_announces_recovery(self, required, monkeypatch, caplog):
        required.book(align_method="lexical")
        required.db.add_pending_shelf_add("a1", None)
        get_book = required.db.get_book
        monkeypatch.setattr(required.db, "get_book", MagicMock(side_effect=OSError("database is locked")))

        with caplog.at_level(logging.DEBUG, logger=web_server.logger.name):
            web_server._reconcile_aligned_shelf()
            web_server._reconcile_aligned_shelf()
            monkeypatch.setattr(required.db, "get_book", get_book)
            assert web_server._reconcile_aligned_shelf() == 1

        failures = [r for r in caplog.records if "Aligned-shelf reconcile failed" in r.getMessage()]
        assert [r.levelno for r in failures] == [logging.WARNING, logging.DEBUG]
        assert failures[0].exc_info is not None
        assert any("recovered after 2 occurrences" in r.getMessage() for r in caplog.records)

    def test_inactive_reader_waits_and_deleted_reader_is_forgotten(self, required):
        required.book(align_method="lexical")
        inactive, inactive_client = required.user("inactive", active=False)
        required.db.add_pending_shelf_add("a1", inactive)
        required.db.add_pending_shelf_add("a1", 999)

        assert web_server._reconcile_aligned_shelf() == 0

        inactive_client.add_book_id_to_shelf.assert_not_called()
        assert required.db.get_pending_shelf_adds() == [("a1", inactive)]

    def test_a_book_that_left_grimmory_is_forgotten(self, required):
        required.book(source="BookOrbit", align_method="lexical")
        required.db.add_pending_shelf_add("a1", None)

        assert web_server._reconcile_aligned_shelf() == 0

        required.global_client.add_book_id_to_shelf.assert_not_called()
        assert required.db.get_pending_shelf_adds() == []

    def test_unconfigured_client_keeps_the_book_waiting(self, required):
        required.book(align_method="lexical")
        required.db.add_pending_shelf_add("a1", None)
        required.global_client.is_configured.return_value = False

        assert web_server._reconcile_aligned_shelf() == 0

        assert required.db.get_pending_shelf_adds() == [("a1", None)]

    def test_errors_are_swallowed(self, required, monkeypatch):
        monkeypatch.setattr(required.db, "get_pending_shelf_adds", MagicMock(side_effect=RuntimeError("boom")))

        assert web_server._reconcile_aligned_shelf() is None


class TestDeletedMatchLeavesTheShelf:
    def test_aligned_add_is_removed_from_recorded_owner_after_settings_change(
            self, required, tmp_path, monkeypatch):
        book = required.book(align_method="lexical")
        reader, reader_client = required.user("reader", shelf_name="Reader shelf")
        owner, owner_client = required.owner()
        required.db.add_pending_shelf_add("a1", reader)

        assert web_server._reconcile_aligned_shelf() == 1
        book.ebook_source_id = "remapped-ebook"
        monkeypatch.setenv("BOOKLORE_SHELF_REQUIRE_ALIGNMENT", "false")
        monkeypatch.delenv("BOOKLORE_SHELF_OWNER")
        monkeypatch.setenv("BOOKLORE_SHELF_NAME", "Renamed shelf")
        required.acting_as(reader)
        required.cleanup(book, tmp_path)

        owner_client.remove_book_id_from_shelf.assert_called_once_with("45", "ABS Synced")
        reader_client.remove_book_id_from_shelf.assert_not_called()
        reader_client.remove_from_shelf.assert_not_called()
        assert required.db.get_shelf_assignments("a1") == []

    def test_shared_claim_keeps_assignment_until_final_mapping_delete(self, required, tmp_path):
        book = required.book(align_method="lexical")
        leaver, _ = required.user("leaver")
        keeper, _ = required.user("keeper")
        owner, owner_client = required.owner()
        required.db.link_user_book(leaver, "a1")
        required.db.link_user_book(keeper, "a1")
        required.db.add_pending_shelf_add("a1", leaver)
        assert web_server._reconcile_aligned_shelf() == 1

        web_server._delete_or_unlink_book(SimpleNamespace(id=leaver), "a1", book)

        assert required.db.get_shelf_assignments("a1") == [{
            "abs_id": "a1", "reader_id": leaver,
            "target_user_id": owner, "shelf_name": "ABS Synced", "source_id": "45",
        }]
        monkeypatch = required.monkeypatch
        monkeypatch.setenv("BOOKLORE_SHELF_REQUIRE_ALIGNMENT", "false")
        monkeypatch.setattr(web_server, "manager", MagicMock(cancel_background_job=MagicMock(return_value=False)))
        monkeypatch.setattr(web_server, "DATA_DIR", tmp_path, raising=False)
        web_server.container.epub_cache_dir.return_value = str(tmp_path)

        web_server._delete_or_unlink_book(SimpleNamespace(id=keeper), "a1", book)

        owner_client.remove_book_id_from_shelf.assert_called_once_with("45", "ABS Synced")
        assert required.db.get_shelf_assignments("a1") == []

    def test_failed_recorded_target_removal_keeps_assignment(self, required, tmp_path):
        book = required.book(align_method="lexical")
        reader, _ = required.user("reader")
        owner, owner_client = required.owner()
        required.db.add_pending_shelf_add("a1", reader)
        assert web_server._reconcile_aligned_shelf() == 1
        owner_client.remove_book_id_from_shelf.return_value = False

        required.cleanup(book, tmp_path)

        owner_client.remove_book_id_from_shelf.assert_called_once_with("45", "ABS Synced")
        assert required.db.get_shelf_assignments("a1") == [{
            "abs_id": "a1", "reader_id": reader,
            "target_user_id": owner, "shelf_name": "ABS Synced", "source_id": "45",
        }]

    def test_reader_removes_by_id_from_their_own_shelf(self, required, tmp_path):
        book = required.book()
        reader, client = required.user("reader", shelf_name="Reading")
        required.acting_as(reader)
        required.db.add_pending_shelf_add("a1", reader)
        required.db.add_pending_shelf_add("a1", None)

        required.cleanup(book, tmp_path)

        client.remove_book_id_from_shelf.assert_called_once_with("45", "Reading")
        required.global_client.remove_book_id_from_shelf.assert_not_called()
        assert required.db.get_pending_shelf_adds() == []

    def test_owner_removes_by_id_from_the_shared_shelf(self, required, tmp_path):
        book = required.book()
        reader, reader_client = required.user("reader", shelf_name="Reading")
        _, owner_client = required.owner()
        required.acting_as(reader)

        required.cleanup(book, tmp_path)

        owner_client.remove_book_id_from_shelf.assert_called_once_with("45", "ABS Synced")
        reader_client.remove_book_id_from_shelf.assert_not_called()
        reader_client.remove_from_shelf.assert_not_called()

    def test_a_book_shelved_at_match_time_leaves_the_deleters_shelf(self, required, tmp_path):
        book = required.book(sync_mode="ebook_only")
        reader, reader_client = required.user("reader")
        _, owner_client = required.owner()
        required.acting_as(reader)

        required.cleanup(book, tmp_path)

        reader_client.remove_from_shelf.assert_called_once_with("a1.epub", "ABS Synced")
        owner_client.remove_book_id_from_shelf.assert_not_called()

    def test_off_removes_from_the_deleters_shelf_and_forgets_the_pending_add(self, shelving, tmp_path):
        book = shelving.book()
        shelving.db.add_pending_shelf_add("a1", None)

        shelving.cleanup(book, tmp_path)

        shelving.global_client.remove_from_shelf.assert_called_once_with("a1.epub", "ABS Synced")
        assert shelving.db.get_pending_shelf_adds() == []

    def test_dropping_a_shared_claim_forgets_only_that_readers_pending_add(self, required):
        book = required.book()
        leaver, _ = required.user("leaver")
        keeper, _ = required.user("keeper")
        for user_id in (leaver, keeper):
            required.db.link_user_book(user_id, "a1")
            required.db.add_pending_shelf_add("a1", user_id)

        web_server._delete_or_unlink_book(SimpleNamespace(id=leaver), "a1", book)

        assert required.db.get_pending_shelf_adds() == [("a1", keeper)]
        assert required.db.get_book("a1") is not None


class TestAddBookIdToShelf:
    def _client(self, shelf_id, response):
        client = BookloreClient.__new__(BookloreClient)
        client._creds = None
        client._get_or_create_shelf_id = MagicMock(return_value=shelf_id)
        client._make_request = MagicMock(return_value=response)
        return client

    def test_assigns_by_id(self):
        client = self._client(18, MagicMock(status_code=200))
        assert client.add_book_id_to_shelf("45", "ABS Synced")
        client._make_request.assert_called_once_with("POST", "/api/v1/books/shelves", {
            "bookIds": [45], "shelvesToAssign": [18], "shelvesToUnassign": []})

    def test_missing_shelf_or_failure_is_false(self):
        assert not self._client(None, MagicMock(status_code=200)).add_book_id_to_shelf("45", "ABS Synced")
        assert not self._client(18, MagicMock(status_code=401)).add_book_id_to_shelf("45", "ABS Synced")
        assert not self._client(18, MagicMock(status_code=200)).add_book_id_to_shelf("", "ABS Synced")


class TestRemoveBookIdFromShelf:
    def _client(self, shelf_id, response):
        client = BookloreClient.__new__(BookloreClient)
        client._creds = None
        client._get_shelf_id = MagicMock(return_value=shelf_id)
        client._make_request = MagicMock(return_value=response)
        return client

    def test_unassigns_by_id(self):
        client = self._client(18, MagicMock(status_code=200))
        assert client.remove_book_id_from_shelf("45", "ABS Synced")
        client._make_request.assert_called_once_with("POST", "/api/v1/books/shelves", {
            "bookIds": [45], "shelvesToAssign": [], "shelvesToUnassign": [18]})

    def test_missing_shelf_or_failure_is_false(self):
        missing = self._client(None, MagicMock(status_code=200))
        assert not missing.remove_book_id_from_shelf("45", "ABS Synced")
        missing._make_request.assert_not_called()
        assert not self._client(18, MagicMock(status_code=403)).remove_book_id_from_shelf("45", "ABS Synced")
        assert not self._client(18, MagicMock(status_code=200)).remove_book_id_from_shelf("", "ABS Synced")
