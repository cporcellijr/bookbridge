import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import src.web_server as web_server
import src.api.kosync_server as kosync_server
from src.api.booklore_client import BookloreClient
from src.db.database_service import DatabaseService
from src.db.models import Book
from src.services.library_service import LibraryService
from src.sync_clients.booklore_sync_client import BookloreSyncClient
from src.sync_clients.sync_client_interface import LocatorResult, UpdateProgressRequest


OLD_CBZ = "Suske en Wiske - 002 - De Vliegende Aap.cbz"
NEW_CBZ = "De vliegende aap - Willy Vandersteen (1996).cbz"


def mapped_book(filename="old-name.epub", source="BookLore", source_id="4"):
    return Book(
        abs_id="book-1",
        abs_title="Book",
        ebook_filename=filename,
        original_ebook_filename=filename,
        ebook_source=source,
        ebook_source_id=source_id,
        kosync_doc_id="stable-kosync-hash",
        status="active",
    )


@pytest.mark.parametrize("source", ["BookLore", "Booklore", "Grimmory"])
def test_mapped_read_uses_stable_id_after_filename_alias_is_removed(source):
    api = MagicMock(spec=BookloreClient)
    api.get_progress_rich_by_book_id.return_value = {
        "pct": 1.0,
        "cfi": None,
        "href": None,
        "last_read_time": None,
        "status": "READ",
    }
    sync = BookloreSyncClient(api, MagicMock())

    state = sync.get_service_state(mapped_book(OLD_CBZ, source), prev_state=None)

    assert state.current["pct"] == 1.0
    api.get_progress_rich_by_book_id.assert_called_once_with("4")
    api.find_book_by_filename.assert_not_called()
    api.find_book_by_filename_exact.assert_not_called()


@pytest.mark.parametrize("filename", ["old-name.epub", OLD_CBZ])
def test_mapped_write_uses_stable_id_for_epub_and_cbz(filename):
    api = MagicMock(spec=BookloreClient)
    api.update_progress_by_book_id.return_value = True
    sync = BookloreSyncClient(api, MagicMock())
    request = UpdateProgressRequest(locator_result=LocatorResult(percentage=1.0))

    result = sync.update_progress(mapped_book(filename), request)

    assert result.success is True
    api.update_progress_by_book_id.assert_called_once_with("4", 1.0, request.locator_result)
    api.update_progress.assert_not_called()
    api.find_book_by_filename.assert_not_called()


def test_legacy_exact_filename_fallback_backfills_source_id_without_fuzzy_match():
    api = MagicMock(spec=BookloreClient)
    api.db = MagicMock()
    api.find_book_by_filename_exact.return_value = {"id": 4, "fileName": "legacy.epub"}
    api.get_progress_rich_by_book_id.return_value = {"pct": 0.25, "cfi": None}
    book = mapped_book("legacy.epub", source="Grimmory", source_id=None)
    sync = BookloreSyncClient(api, MagicMock())

    state = sync.get_service_state(book, prev_state=None)

    assert state.current["pct"] == 0.25
    assert book.ebook_source_id == "4"
    api.find_book_by_filename_exact.assert_called_once_with("legacy.epub")
    api.find_book_by_filename.assert_not_called()
    api.db.backfill_ebook_source_id_if_unclaimed.assert_called_once_with(
        "book-1", "4", "Booklore"
    )


def test_legacy_resolver_rejects_non_grimmory_source_before_lookup():
    api = MagicMock(spec=BookloreClient)
    sync = BookloreSyncClient(api, MagicMock())
    book = mapped_book("same-name.epub", source="BookOrbit", source_id=None)

    assert sync._resolve_legacy_book_id(book, "same-name.epub") is None

    api.find_book_by_filename_exact.assert_not_called()


def bare_client(db, books_by_id):
    client = BookloreClient.__new__(BookloreClient)
    client.db = db
    client._cache_lock = threading.RLock()
    client._book_cache = {
        str(info.get("fileName")).lower(): info
        for info in books_by_id.values()
        if isinstance(info, dict) and info.get("fileName")
    }
    client._book_id_cache = books_by_id
    client._exact_filename_miss_cache = {}
    client._refresh_cooldown = 300
    client._cache_timestamp = time.time()
    client._creds = {}
    return client


def response(status_code=200):
    value = MagicMock()
    value.status_code = status_code
    value.__bool__.return_value = True
    return value


def test_api_rich_read_by_id_is_one_direct_get():
    client = bare_client(None, {})
    client._make_request = MagicMock(return_value=response())
    client._parse_json_response = MagicMock(return_value={
        "id": 4,
        "primaryFile": {"fileName": "new-name.epub", "bookType": "EPUB"},
        "epubProgress": {"percentage": 34.48, "cfi": "epubcfi(/6/4!)"},
    })

    rich = client.get_progress_rich_by_book_id("4")

    assert rich["pct"] == pytest.approx(0.3448)
    client._make_request.assert_called_once_with("GET", "/api/v1/books/4")
    assert client._get_cached_book_by_id("4")["fileName"] == "new-name.epub"


def test_api_write_by_id_keeps_payload_and_verification_without_filename_lookup():
    detail = {
        "id": 4,
        "fileName": "new-name.epub",
        "primaryFile": {"id": 44, "fileName": "new-name.epub", "bookType": "EPUB"},
        "epubProgress": {"percentage": 10.3448},
    }
    client = bare_client(None, {4: detail})
    client._epub_cfi_write_disabled_for_books = set()
    client.find_book_by_filename = MagicMock()
    client._make_request = MagicMock(side_effect=[response(204), response(200)])
    client._parse_json_response = MagicMock(return_value={
        **detail,
        "epubProgress": {"percentage": 100.0},
    })

    assert client.update_progress_by_book_id(4, 1.0, LocatorResult(percentage=1.0)) is True

    first_call = client._make_request.call_args_list[0]
    assert first_call.args[:2] == ("POST", "/api/v1/books/progress")
    assert first_call.args[2] == {
        "bookId": 4,
        "fileProgress": {"bookFileId": 44, "progressPercent": 100.0},
    }
    assert client._make_request.call_args_list[1].args == ("GET", "/api/v1/books/4")
    client.find_book_by_filename.assert_not_called()


def test_rich_read_then_write_reuses_hydrated_detail():
    detail = {
        "id": 4,
        "primaryFile": {"id": 44, "fileName": "new-name.epub", "bookType": "EPUB"},
        "epubProgress": {"percentage": 10.0},
    }
    verified = {**detail, "epubProgress": {"percentage": 100.0}}
    client = bare_client(None, {})
    client._epub_cfi_write_disabled_for_books = set()
    client._make_request = MagicMock(
        side_effect=[response(200), response(204), response(200)]
    )
    client._parse_json_response = MagicMock(side_effect=[detail, verified])

    assert client.get_progress_rich_by_book_id("4")["pct"] == pytest.approx(0.1)
    assert client.update_progress_by_book_id(
        "4", 1.0, LocatorResult(percentage=1.0)
    ) is True

    assert [call.args[:2] for call in client._make_request.call_args_list] == [
        ("GET", "/api/v1/books/4"),
        ("POST", "/api/v1/books/progress"),
        ("GET", "/api/v1/books/4"),
    ]


def test_cold_detail_lookup_accepts_string_mapping_id_and_integer_api_id():
    client = bare_client(None, {})
    client._get_fresh_token = MagicMock(return_value="token")
    client._fetch_book_detail = MagicMock(return_value={"id": 4})

    def cache_integer_id(_detail):
        client._book_id_cache[4] = {"id": 4, "fileName": "new-name.epub"}

    client._process_book_detail = cache_integer_id

    assert client._fetch_and_cache_detail("4") == {
        "id": 4,
        "fileName": "new-name.epub",
    }


def test_failed_direct_rich_read_does_not_issue_duplicate_classic_get():
    api = MagicMock(spec=BookloreClient)
    api.get_progress_rich_by_book_id.return_value = None
    sync = BookloreSyncClient(api, MagicMock())

    assert sync.get_service_state(mapped_book(), prev_state=None) is None

    api.get_progress_rich_by_book_id.assert_called_once_with("4")
    api.get_progress_by_book_id.assert_not_called()


@pytest.mark.parametrize(
    ("old_filename", "new_filename"),
    [("old-name.epub", "new-name.epub"), (OLD_CBZ, NEW_CBZ)],
)
def test_library_reconcile_refreshes_only_external_filename(old_filename, new_filename):
    book = mapped_book(old_filename)
    db = MagicMock()
    db.get_all_books.return_value = [book]
    db.update_book_fields.return_value = True
    client = bare_client(db, {4: {"id": 4, "fileName": new_filename}})

    assert client.reconcile_mapping_filename_drift() == 1

    db.update_book_fields.assert_called_once_with(
        "book-1",
        expected_ebook_source_id="4",
        expected_grimmory_source=True,
        notify_catalog_change=True,
        ebook_filename=new_filename,
    )
    assert book.ebook_filename == old_filename
    assert book.original_ebook_filename == old_filename
    assert book.ebook_source_id == "4"
    assert book.kosync_doc_id == "stable-kosync-hash"
    assert book.status == "active"


def test_library_reconcile_does_not_relink_missing_known_id_by_filename():
    book = mapped_book("same-name.epub", source_id="404")
    db = MagicMock()
    db.get_all_books.return_value = [book]
    client = bare_client(db, {5: {"id": 5, "fileName": "same-name.epub"}})

    assert client.reconcile_mapping_filename_drift() == 0

    assert book.ebook_source_id == "404"
    assert book.ebook_filename == "same-name.epub"
    db.update_book_fields.assert_not_called()


def test_library_reconcile_backfills_only_unambiguous_exact_legacy_match():
    exact = mapped_book("exact.epub", source="booklore", source_id=None)
    ambiguous = mapped_book("duplicate.epub", source="Grimmory", source_id=None)
    ambiguous.abs_id = "book-2"
    db = MagicMock()
    db.get_all_books.return_value = [exact, ambiguous]
    db.backfill_ebook_source_id_if_unclaimed.return_value = True
    client = bare_client(db, {
        4: {"id": 4, "fileName": "exact.epub"},
        5: {"id": 5, "fileName": "duplicate.epub"},
        6: {"id": 6, "fileName": "duplicate.epub"},
    })

    assert client.reconcile_mapping_filename_drift() == 1

    assert exact.ebook_source_id == "4"
    assert ambiguous.ebook_source_id is None
    db.backfill_ebook_source_id_if_unclaimed.assert_called_once_with(
        "book-1", "4", "Booklore"
    )


def test_library_reconcile_rejects_two_legacy_mappings_claiming_one_source_id():
    first = mapped_book("duplicate.epub", source="booklore", source_id=None)
    second = mapped_book("duplicate.epub", source="Grimmory", source_id=None)
    second.abs_id = "book-2"
    db = MagicMock()
    db.get_all_books.return_value = [first, second]
    client = bare_client(db, {4: {"id": 4, "fileName": "duplicate.epub"}})

    assert client.reconcile_mapping_filename_drift() == 0

    db.backfill_ebook_source_id_if_unclaimed.assert_not_called()
    assert first.ebook_source_id is None
    assert second.ebook_source_id is None


def test_library_reconcile_does_not_backfill_with_incomplete_filename_catalog():
    legacy = mapped_book("exact.epub", source="Grimmory", source_id=None)
    db = MagicMock()
    db.get_all_books.return_value = [legacy]
    client = bare_client(db, {
        4: {"id": 4, "fileName": "exact.epub"},
        5: {"id": 5, "fileName": None, "_needs_detail": True},
    })

    assert client.reconcile_mapping_filename_drift() == 0

    db.backfill_ebook_source_id_if_unclaimed.assert_not_called()


def test_reconcile_narrow_write_preserves_concurrent_sync_fields(tmp_path):
    db = DatabaseService(str(tmp_path / "reconcile.db"))
    try:
        db.save_book(mapped_book("old-name.epub"))
        stale_snapshot = db.get_book("book-1")

        current = db.get_book("book-1")
        current.status = "processing"
        current.kosync_doc_id = "concurrent-kosync-hash"
        current.transcript_file = "concurrent.json"
        db.save_book(current)

        db.get_all_books = MagicMock(return_value=[stale_snapshot])
        client = bare_client(db, {4: {"id": 4, "fileName": "new-name.epub"}})

        assert client.reconcile_mapping_filename_drift() == 1

        saved = db.get_book("book-1")
        assert saved.ebook_filename == "new-name.epub"
        assert saved.status == "processing"
        assert saved.kosync_doc_id == "concurrent-kosync-hash"
        assert saved.transcript_file == "concurrent.json"
    finally:
        db.db_manager.close()


def test_reconcile_conditional_write_does_not_touch_concurrently_remapped_book(tmp_path):
    db = DatabaseService(str(tmp_path / "conditional-reconcile.db"))
    try:
        db.save_book(mapped_book("old-name.epub"))
        stale_snapshot = db.get_book("book-1")

        current = db.get_book("book-1")
        current.ebook_source_id = "99"
        current.ebook_filename = "other-book.epub"
        db.save_book(current)

        db.get_all_books = MagicMock(return_value=[stale_snapshot])
        client = bare_client(db, {4: {"id": 4, "fileName": "new-name.epub"}})

        assert client.reconcile_mapping_filename_drift() == 0

        saved = db.get_book("book-1")
        assert saved.ebook_source_id == "99"
        assert saved.ebook_filename == "other-book.epub"
    finally:
        db.db_manager.close()


def test_database_backfill_allows_only_one_mapping_to_claim_source_id(tmp_path):
    db = DatabaseService(str(tmp_path / "source-claim.db"))
    try:
        first = mapped_book("duplicate.epub", source="Grimmory", source_id=None)
        second = mapped_book("duplicate.epub", source="booklore", source_id=None)
        second.abs_id = "book-2"
        db.save_book(first)
        db.save_book(second)

        assert db.backfill_ebook_source_id_if_unclaimed("book-1", "4", "Booklore")
        assert not db.backfill_ebook_source_id_if_unclaimed("book-2", "4", "Booklore")

        assert db.get_book("book-1").ebook_source_id == "4"
        assert db.get_book("book-2").ebook_source_id is None
    finally:
        db.db_manager.close()


@pytest.mark.parametrize("stored_source", ["BookLore", "Booklore", "Grimmory", "grimmory"])
def test_database_source_lookup_normalizes_grimmory_aliases(tmp_path, stored_source):
    db = DatabaseService(str(tmp_path / "source-lookup.db"))
    try:
        db.save_book(mapped_book(source=stored_source, source_id="4"))

        found = db.get_book_by_ebook_source("BookLore", "4")

        assert found is not None
        assert found.abs_id == "book-1"
    finally:
        db.db_manager.close()


def test_exact_filename_lookup_uses_hourly_refresh_and_negative_cache():
    client = bare_client(None, {4: {"id": 4, "fileName": "present.epub"}})
    client._is_refresh_on_cooldown = MagicMock(return_value=False)
    client._refresh_book_cache = MagicMock(return_value=True)
    client._cache_timestamp = time.time() - 120

    assert client.find_book_by_filename_exact("missing.epub") is None
    assert client.find_book_by_filename_exact("missing.epub") is None
    client._refresh_book_cache.assert_not_called()

    client._exact_filename_miss_cache.clear()
    client._cache_timestamp = time.time() - 3601
    assert client.find_book_by_filename_exact("missing.epub") is None
    assert client.find_book_by_filename_exact("missing.epub") is None
    client._refresh_book_cache.assert_called_once_with()


def test_exact_filename_lookup_rejects_duplicate_catalog_ids():
    first = {"id": 4, "fileName": "duplicate.epub"}
    second = {"id": 5, "fileName": "duplicate.epub"}
    client = bare_client(None, {4: first, 5: second})

    assert client.find_book_by_filename_exact("duplicate.epub", allow_refresh=False) is None


def test_local_cached_filename_survives_external_rename(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    old_cache = cache_dir / "old-name.epub"
    old_cache.write_bytes(b"cached" * 300)
    book = mapped_book("new-name.epub")
    book.original_ebook_filename = "old-name.epub"
    api = MagicMock()
    api.is_configured.return_value = True
    service = LibraryService(MagicMock(), api, MagicMock(), MagicMock(), str(cache_dir))

    result = service.acquire_ebook({}, book)

    assert result == str(old_cache)
    api.download_book.assert_not_called()
    assert book.kosync_doc_id == "stable-kosync-hash"


def test_scheduled_library_sync_runs_mapping_reconciliation():
    api = MagicMock()
    api.is_configured.return_value = True
    api.get_all_books.return_value = []
    service = LibraryService(MagicMock(), api, MagicMock(), MagicMock(), "/tmp/cache")
    service.get_syncable_books = MagicMock(return_value=[])

    service.sync_library_books()

    api.get_all_books.assert_called_once_with()
    api.reconcile_mapping_filename_drift.assert_called_once_with()


def test_same_source_and_id_filename_change_keeps_active(monkeypatch):
    db = MagicMock()
    db.has_alignment.return_value = True
    monkeypatch.setattr(web_server, "database_service", db)
    book = mapped_book("old-name.epub", source="Grimmory", source_id="4")

    web_server._preserve_or_reset_mapping_status(
        book,
        ebook_filename="new-name.epub",
        ebook_source="BookLore",
        ebook_source_id="4",
    )

    assert book.status == "active"


def test_same_source_and_id_ebook_only_mapping_keeps_active_without_alignment(monkeypatch):
    db = MagicMock()
    db.has_alignment.return_value = False
    monkeypatch.setattr(web_server, "database_service", db)
    book = mapped_book("old-name.epub", source="grimmory", source_id="4")
    book.sync_mode = "ebook_only"

    web_server._preserve_or_reset_mapping_status(
        book,
        ebook_filename="new-name.epub",
        ebook_source="Booklore",
        ebook_source_id="4",
    )

    assert book.status == "active"
    db.has_alignment.assert_not_called()


@pytest.mark.parametrize("source", ["grimmory", "Booklore", "BookLore"])
def test_kosync_auto_map_keeps_grimmory_id_for_all_source_spellings(monkeypatch, source):
    mapping_service = MagicMock()
    saved = SimpleNamespace(
        abs_id="book-1",
        ebook_source=source,
        ebook_source_id="4",
    )
    mapping_service.create_audio_mapping_from_match.return_value = saved
    container = MagicMock()
    container.book_mapping_service.return_value = mapping_service
    database = MagicMock()
    monkeypatch.setattr(kosync_server, "_container", container)
    monkeypatch.setattr(kosync_server, "_database_service", database)
    monkeypatch.setattr(kosync_server, "_manager", None)
    monkeypatch.setattr(
        kosync_server,
        "_resolve_library_ebook_source",
        lambda _filename: (source, "4"),
    )

    result = kosync_server._auto_map_ebook_to_audiobook(
        "hash", "renamed.epub", {"abs_id": "audio-1", "title": "Book"}, "test"
    )

    assert result is saved
    assert mapping_service.create_audio_mapping_from_match.call_args.kwargs[
        "booklore_ebook_id"
    ] == "4"


@pytest.mark.parametrize(
    ("new_source", "new_id"),
    [("Grimmory", "5"), ("BookOrbit", "4")],
)
def test_changed_source_identity_still_requeues(monkeypatch, new_source, new_id):
    db = MagicMock()
    db.has_alignment.return_value = True
    monkeypatch.setattr(web_server, "database_service", db)
    book = mapped_book("old-name.epub", source="BookLore", source_id="4")

    web_server._preserve_or_reset_mapping_status(
        book,
        ebook_filename="new-name.epub",
        ebook_source=new_source,
        ebook_source_id=new_id,
    )

    assert book.status == "pending"
    db.has_alignment.assert_not_called()
