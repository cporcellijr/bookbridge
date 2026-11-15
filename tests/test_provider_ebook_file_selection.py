"""Different EPUBs in one provider entry are different coordinate spaces."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask, jsonify

from src.api.booklore_client import BookloreClient
from src.api.bookorbit_client import BookOrbitClient
from src.utils.provider_ebook_files import ebook_files, hash_selected_ebook, mapped_ebook_selection, same_ebook_selection, select_ebook_file
from src.web.routes import integration_lookups


def orbit_detail():
    return {"id": 7, "files": [
        {"id": 10, "filename": "plain.epub", "format": "epub", "role": "primary"},
        {"id": 20, "filename": "readaloud.epub", "format": "epub", "mediaOverlayAvailable": True},
        {"id": 30, "filename": "original.m4b", "format": "m4b"},
    ]}


def grimmory_detail():
    return {"id": 7, "primaryFile": {"id": 10, "fileName": "plain.epub", "bookType": "EPUB"},
            "alternativeFormats": [{"id": 20, "fileName": "readaloud.epub", "bookType": "EPUB"}],
            "supplementaryFiles": [{"id": 30, "fileName": "cover.jpg"}]}


def response(body=None, status=200):
    result = MagicMock(status_code=status, content=b"selected bytes")
    result.json.return_value = body
    result.__bool__.return_value = status < 400
    return result


@pytest.mark.parametrize("source,detail", [("BookOrbit", orbit_detail), ("Booklore", grimmory_detail)])
def test_files_are_distinct_book_scoped_choices(source, detail):
    files = ebook_files(detail(), source)
    assert [(f["id"], f["filename"]) for f in files] == [(10, "plain.epub"), (20, "readaloud.epub")]
    assert files[0]["primary"] is True
    assert files[1]["primary"] is False
    assert select_ebook_file(detail(), source, "readaloud.epub")["id"] == 20
    assert select_ebook_file(detail(), source, "missing.epub") is None


def test_rejects_ambiguous_names_and_non_basename_provider_files():
    detail = orbit_detail()
    detail["files"].extend([
        {"id": 40, "filename": "../escape.epub"},
        {"id": 41, "filename": "C:\\escape.epub"},
        {"id": 42, "filename": "readaloud.epub"},
    ])
    assert len(ebook_files(detail, "BookOrbit")) == 3
    assert select_ebook_file(detail, "BookOrbit", "readaloud.epub") is None


def test_legacy_grimmory_primary_filename_remains_supported():
    selected = select_ebook_file({"id": 7, "fileName": "plain.epub"}, "Booklore", "plain.epub")
    assert selected["primary"] is True
    assert selected["id"] is None


def test_another_publication_does_not_reuse_the_old_documents_hash():
    mapping = SimpleNamespace(ebook_source="BookOrbit", ebook_source_id="7",
                              original_ebook_filename="plain.epub", ebook_filename="plain.epub")
    assert same_ebook_selection(mapping, "plain.epub", "BookOrbit", 7, 10)
    assert not same_ebook_selection(mapping, "readaloud.epub", "BookOrbit", 7, 20)
    assert not same_ebook_selection(mapping, "plain.epub", "BookOrbit", 8, 10)
    assert not same_ebook_selection(mapping, "plain.epub", "Grimmory", 7, 10)


def test_grimmory_filename_drift_does_not_replace_a_selected_alternate():
    db = MagicMock()
    mapping = SimpleNamespace(abs_id="abs7", ebook_source="Grimmory", ebook_source_id="7",
                              ebook_filename="readaloud.epub", original_ebook_filename="readaloud.epub", ebook_source_file_id="20")
    db.get_all_books.return_value = [mapping]
    client = BookloreClient(database_service=db)
    remote = dict(grimmory_detail(), fileName="plain.epub")
    with patch.object(client, "_snapshot_book_id_items", return_value=[(7, remote)]):
        assert client.reconcile_mapping_filename_drift() == 0
    db.update_book_fields.assert_not_called()


def test_deleted_file_is_not_replaced_by_a_different_file_with_the_same_name():
    assert select_ebook_file(orbit_detail(), "BookOrbit", "readaloud.epub", file_id=999) is None


def test_new_file_selection_survives_database_roundtrip(tmp_path):
    from src.db.database_service import DatabaseService
    from src.db.models import Book
    db = DatabaseService(str(tmp_path / "books.db"))
    db.save_book(Book(abs_id="abs7", ebook_filename="readaloud.epub", ebook_source="BookOrbit",
                      ebook_source_id="7", ebook_source_file_id="20"))
    assert db.get_book("abs7").ebook_source_file_id == "20"
    db.save_book(Book(abs_id="abs7", ebook_filename="other.epub", ebook_source="BookOrbit",
                      ebook_source_id="7", ebook_source_file_id="21"))
    assert db.get_book("abs7").ebook_source_file_id == "21"


def test_legacy_rows_do_not_opt_into_file_pinning():
    assert mapped_ebook_selection(SimpleNamespace(ebook_source_file_id=None)) == {}
    assert mapped_ebook_selection(MagicMock()) == {}


def test_hashing_streams_selected_file_and_keeps_its_original_basename():
    client, parser = MagicMock(), MagicMock()
    def download(book_id, filename, target, *, ebook_file_id):
        assert (book_id, filename, ebook_file_id) == (7, "readaloud.epub", "20")
        target.write_bytes(b"publication")
        return True
    def hash_file(path):
        assert path.name == "readaloud.epub"
        assert path.read_bytes() == b"publication"
        return "selected-hash"
    client.download_ebook_to_path.side_effect = download
    parser.get_kosync_id.side_effect = hash_file
    assert hash_selected_ebook(client, parser, 7, "readaloud.epub", "20") == "selected-hash"
    client.download_book.assert_not_called()


def test_migration_preserves_rows_and_is_idempotent(monkeypatch):
    import importlib.util
    from pathlib import Path
    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    path = Path(__file__).resolve().parents[1] / "alembic/versions/d7e4a9b2c681_add_ebook_source_file_id.py"
    spec = importlib.util.spec_from_file_location("ebook_file_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE books (abs_id VARCHAR PRIMARY KEY)")
        connection.exec_driver_sql("INSERT INTO books VALUES ('existing')")
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        migration.upgrade()
        assert connection.exec_driver_sql("SELECT abs_id, ebook_source_file_id FROM books").all() == [("existing", None)]
        migration.downgrade()
        migration.downgrade()
        assert connection.exec_driver_sql("SELECT abs_id FROM books").all() == [("existing",)]


def test_grimmory_primary_cfi_is_ambiguous_when_two_epubs_exist():
    client = BookloreClient()
    detail = grimmory_detail()
    detail["epubProgress"] = {"percentage": 60, "cfi": "unknown-source-dom"}
    with patch.object(client, "_make_request", return_value=response(detail)):
        assert client.get_progress_rich_by_book_id(7, ebook_filename="plain.epub", ebook_file_id="10") is None


def test_orbit_search_expands_files_only_when_requested():
    client = BookOrbitClient()
    with patch.object(client, "_search_raw", return_value=[{"id": 7, "title": "Title", "formats": ["epub"]}]), \
         patch.object(client, "get_book_detail", return_value=orbit_detail()):
        assert len(client.search_ebooks("Title")) == 1
        files = client.search_ebooks("Title", include_files=True)
    assert [(f["fileName"], f["sourceFileId"]) for f in files] == [("plain.epub", 10), ("readaloud.epub", 20)]


def test_orbit_downloads_the_selected_file_even_after_primary_changes():
    client = BookOrbitClient()
    with patch.object(client, "get_book_detail", return_value=orbit_detail()) as detail, \
         patch.object(client, "_make_request", return_value=response()) as request:
        assert client.download_book(7, ebook_filename="readaloud.epub") == b"selected bytes"
    detail.assert_called_once_with(7, force=True, fresh_only=True)
    request.assert_called_once_with("GET", "/api/v1/books/files/20/download")


def test_orbit_missing_selection_never_falls_back_to_primary():
    client = BookOrbitClient()
    with patch.object(client, "get_book_detail", return_value=orbit_detail()), \
         patch.object(client, "_make_request") as request:
        assert client.download_book(7, ebook_filename="removed.epub") is None
        assert not client.update_ebook_progress({"id": 7, "ebookFileId": 10, "bridgeEbookFilename": "removed.epub"}, 0.3)
        assert client.get_ebook_progress_rich(7, ebook_filename="removed.epub") is None
    request.assert_not_called()


def test_orbit_reads_and_writes_only_selected_file_progress():
    client = BookOrbitClient()
    rows = [{"fileId": 10, "percentage": 80, "cfi": "wrong-dom"},
            {"fileId": 20, "percentage": 20, "cfi": "right-dom"}]
    with patch.object(client, "get_book_detail", return_value=orbit_detail()), \
         patch.object(client, "_make_request", return_value=response(rows)):
        progress = client.get_ebook_progress_rich(7, ebook_filename="readaloud.epub")
    assert progress["pct"] == 0.2
    assert progress["cfi"] == "right-dom"
    assert progress["file_id"] == 20
    with patch.object(client, "get_book_detail", return_value=orbit_detail()), \
         patch.object(client, "_make_request", return_value=response({"results": [{"status": "saved"}]})) as request:
        assert client.update_ebook_progress({"id": 7, "bridgeEbookFilename": "readaloud.epub"}, 0.3)
    assert request.call_args.args[2]["writes"][0]["fileId"] == 20


def test_grimmory_download_uses_book_and_file_ids():
    client = BookloreClient()
    with patch.object(client, "_get_fresh_token", return_value="private"), \
         patch.object(client, "_make_request", return_value=response(grimmory_detail())), \
         patch.object(client.session, "get", return_value=response()) as request:
        assert client.download_book(7, ebook_filename="readaloud.epub") == b"selected bytes"
    assert request.call_args.args[0].endswith("/api/v1/books/7/files/20/download")


def test_grimmory_missing_selection_cannot_download_primary():
    client = BookloreClient()
    with patch.object(client, "_get_fresh_token", return_value="private"), \
         patch.object(client, "_make_request", return_value=response(grimmory_detail())), \
         patch.object(client.session, "get") as request:
        assert client.download_book(7, ebook_filename="removed.epub") is None
    request.assert_not_called()


def test_grimmory_does_not_read_primary_cfi_against_alternate_text():
    client = BookloreClient()
    detail = grimmory_detail()
    detail["epubProgress"] = {"percentage": 60, "cfi": "primary-dom"}
    with patch.object(client, "_make_request", return_value=response(detail)):
        assert client.get_progress_rich_by_book_id(7, ebook_filename="readaloud.epub") is None


@pytest.mark.parametrize("source", ["BookOrbit", "Grimmory"])
def test_file_endpoint_uses_acting_users_client_not_global_client(monkeypatch, source):
    client = MagicMock()
    client.get_book_detail.return_value = orbit_detail()
    client._make_request.return_value = response(grimmory_detail())
    client._parse_json_response.return_value = grimmory_detail()
    web = SimpleNamespace(jsonify=jsonify, uc=lambda: SimpleNamespace(bookorbit_client=client, booklore_client=client))
    monkeypatch.setattr(integration_lookups, "_WEB_MODULE", web)
    with Flask(__name__).test_request_context():
        result = integration_lookups.get_provider_ebook_files(source, 7)
    assert [f["id"] for f in result.json["files"]] == [10, 20]


def test_file_endpoint_never_falls_back_when_reader_connections_fail(monkeypatch):
    def unavailable():
        raise PermissionError()
    monkeypatch.setattr(integration_lookups, "_WEB_MODULE", SimpleNamespace(jsonify=jsonify, uc=unavailable))
    with Flask(__name__).test_request_context():
        _, status = integration_lookups.get_provider_ebook_files("BookOrbit", 7)
    assert status == 403


def test_rematch_to_another_file_without_a_pick_drops_the_stale_pin():
    from src.db.models import Book
    from tests.test_book_mapping_service import _build_service
    pinned = Book(abs_id="abs-item-1", ebook_filename="readaloud.epub", original_ebook_filename="readaloud.epub",
                  ebook_source="BookLore", ebook_source_id="123", ebook_source_file_id="20", kosync_doc_id="old")
    svc, *_ = _build_service(db_existing=pinned)
    saved = svc.create_audio_mapping_from_match(
        audio_source="ABS", audio_source_id="abs-item-1", audio_title="Book", ebook_filename="other.epub",
        audio_duration=3600.0, ebook_source="BookLore", ebook_source_id="124", booklore_ebook_id="124",
    )
    assert saved.ebook_filename == "other.epub"
    assert saved.ebook_source_file_id is None


def test_pin_survives_a_refresh_of_the_same_file_and_follows_a_new_pick():
    from src.utils.provider_ebook_files import apply_ebook_file_selection
    book = SimpleNamespace(original_ebook_filename="readaloud.epub", ebook_filename="readaloud.epub",
                           ebook_source_id="7", ebook_source_file_id="20")
    apply_ebook_file_selection(book, "readaloud.epub", "7", None)
    assert book.ebook_source_file_id == "20"
    apply_ebook_file_selection(book, None, None, None)
    assert book.ebook_source_file_id == "20"
    apply_ebook_file_selection(book, "readaloud.epub", "8", None)
    assert book.ebook_source_file_id is None
    apply_ebook_file_selection(book, "plain.epub", "7", 10)
    assert book.ebook_source_file_id == "10"


@pytest.mark.parametrize("source,detail", [("BookOrbit", orbit_detail), ("Grimmory", grimmory_detail)])
def test_only_a_book_with_a_choice_of_ebooks_is_pinned(source, detail):
    from src.utils.provider_ebook_files import explicit_ebook_file_id
    client = MagicMock()
    clients = SimpleNamespace(bookorbit_client=client, booklore_client=client)
    single = {"id": 7, "files": [{"id": 10, "filename": "plain.epub", "role": "primary"}]} if source == "BookOrbit" \
        else {"id": 7, "primaryFile": {"id": 10, "fileName": "plain.epub"}}
    for body, file_id, expected in ((detail(), 20, "20"), (single, 10, None), (None, 10, "10")):
        client.get_book_detail.return_value = body
        client._make_request.return_value = response(body, 200 if body else 500)
        client._parse_json_response.return_value = body
        assert explicit_ebook_file_id(clients, source, 7, file_id) == expected
    assert explicit_ebook_file_id(clients, source, 7, None) is None


def test_grimmory_alternate_progress_warning_does_not_repeat_every_cycle(caplog):
    import logging
    from src.utils.logging_utils import get_persistent_condition_logger
    get_persistent_condition_logger().reset()
    client = BookloreClient()
    with patch.object(client, "_make_request", return_value=response(grimmory_detail())), \
         caplog.at_level(logging.DEBUG, logger="src.api.booklore_client"):
        for _ in range(3):
            assert client.get_progress_rich_by_book_id(7, ebook_filename="readaloud.epub") is None
    loud = [r for r in caplog.records if r.levelno >= logging.WARNING and "cannot identify progress" in r.getMessage()]
    assert len(loud) == 1
    get_persistent_condition_logger().reset()


def test_grimmory_alternate_write_is_skipped_but_a_missing_file_fails():
    from src.api.booklore_client import ProgressWriteOutcome
    client = BookloreClient()
    with patch.object(client, "get_book_by_id", return_value={"id": 7, "fileName": "plain.epub"}), \
         patch.object(client, "_make_request", return_value=response(grimmory_detail())):
        assert client.update_progress_by_book_id(7, 0.5, ebook_filename="readaloud.epub", ebook_file_id="20") \
            is ProgressWriteOutcome.SKIPPED
        assert client.update_progress_by_book_id(7, 0.5, ebook_filename="removed.epub", ebook_file_id="99") is False


def test_grimmory_annotation_sync_downloads_the_pinned_file(tmp_path):
    from src.services.annotation_sync_service import AnnotationSyncService
    pinned = SimpleNamespace(abs_id="abs7", ebook_source="Grimmory", ebook_source_id="7", kosync_doc_id="a" * 32,
                             original_ebook_filename="readaloud.epub", ebook_filename="readaloud.epub",
                             ebook_source_file_id="20", abs_title="Book")
    db = MagicMock()
    db.get_linked_abs_ids.return_value = ["abs7"]
    db.get_books_by_status.return_value = [pinned]
    parser = MagicMock()
    parser.resolve_book_path.side_effect = FileNotFoundError
    service = AnnotationSyncService(db, ebook_parser=parser, epub_cache_dir=tmp_path)
    [candidate] = service._candidate_booklore_books(1)
    client = MagicMock()
    client.download_book.return_value = b"alternate bytes"
    service._resolve_booklore_epub_path(client, candidate)
    client.download_book.assert_called_once_with("7", ebook_filename="readaloud.epub", ebook_file_id="20")
