"""Tests for the persisted Suggestions redesign API leaf routes."""

import copy
import threading
from unittest.mock import MagicMock, patch

from flask import Flask
import pytest
from types import SimpleNamespace


@pytest.fixture(autouse=True)
def restore_web_helpers():
    from src import web_server as web
    names = (
        "MATCH_QUEUE_LOCK", "SUGGESTIONS_CACHE_LOCK", "_read_match_queue_unlocked",
        "_match_queue_scope", "_match_queue_item_visible", "_match_queue_stamp",
        "_queue_item_from_suggestion", "_write_match_queue_unlocked",
        "_save_persisted_suggestions_cache", "_safe_local_source_path",
        "get_kosync_id_for_ebook", "_upsert_storyteller_mapping",
        "_claim_book_for_user_id", "container",
    )
    saved = {name: getattr(web, name) for name in names}
    yield
    for name, value in saved.items():
        setattr(web, name, value)


from src.api.suggestions_redesign import init_suggestions_redesign_routes


def _pair_payload(generation="scan-1"):
    return {
        "scan_generation": generation,
        "scanned_at": 100,
        "scan_cache_by_abs": {
            "abs-1": {
                "bridge_key": "abs-1",
                "abs_id": "abs-1",
                "audio_source": "ABS",
                "audio_source_id": "abs-1",
                "audio_title": "The Book",
                "audio_author": "An Author",
                "matches": [{
                    "ebook_filename": "/books/the-book.epub",
                    "display_name": "The Book",
                    "source": "BookOrbit",
                    "source_id": "ebook-1",
                    "source_path": "/books/the-book.epub",
                    "score": 100,
                }],
            },
        },
        "ebook_discovery": {
            "generation": "ebook-1",
            "state": "complete",
            "entries": {},
        },
        "ebook_ignored": {},
    }


def _app_and_patches():
    from src import web_server as web

    app = Flask(__name__)
    app.secret_key = "test"
    init_suggestions_redesign_routes(app)
    db = MagicMock()
    db.get_all_books.return_value = []
    db.get_book.return_value = None
    db.get_kosync_document.return_value = None
    patches = [
        patch.object(web, "get_current_user_id", return_value=7),
        patch.object(web, "database_service", db),
        patch.object(web, "_load_persisted_suggestions_cache", return_value=_pair_payload()),
        patch.object(web, "_load_match_queue", return_value=[]),
        patch.object(web, "_get_ignored_suggestion_source_ids", return_value=[]),
        patch.object(web, "_suggestion_bulk_block", return_value=None),
        patch.object(web, "_running_suggestions_scan_job_id", return_value=None),
        patch.object(web, "_dashboard_visible_books_for_user", side_effect=lambda books, _user: books),
        patch.object(web, "current_user", return_value=MagicMock(id=7, is_admin=True)),
    ]
    started = [item.start() for item in patches]
    app.testing = True
    return app, web, db, patches


def _close(patches):
    for item in reversed(patches):
        item.stop()


def test_discovery_reads_persisted_data_without_provider_calls_and_is_admin_only():
    app, web, _db, patches = _app_and_patches()
    try:
        with patch.object(web, "_get_suggestions_service", side_effect=AssertionError("provider/service read")):
            response = app.test_client().get("/api/suggestions/discovery?tab=pairs")
        assert response.status_code == 200
        payload = response.get_json()
        assert payload["generation"] == "scan-1"
        assert payload["entries"][0]["bridge_key"] == "abs-1"
        assert "suggestions_redesign_discovery" in web._ADMIN_ONLY_ENDPOINTS
    finally:
        _close(patches)


def test_discovery_rejects_unknown_query_parameters():
    app, _web, _db, patches = _app_and_patches()
    try:
        response = app.test_client().get("/api/suggestions/discovery?scope=all")
        assert response.status_code == 400
    finally:
        _close(patches)


def test_pair_selection_rejects_stale_generation():
    app, _web, _db, patches = _app_and_patches()
    try:
        response = app.test_client().post(
            "/api/suggestions/selection/resolve",
            json={"generation": "old", "suggestion_keys": ["pair:abs-1"]},
        )
        assert response.status_code == 409
        assert response.get_json()["error"] == "stale_generation"
    finally:
        _close(patches)


def test_queue_selection_is_revalidated_and_written_once_preserving_other_users():
    app, web, _db, patches = _app_and_patches()
    foreign = {"bridge_key": "other-user", "user_id": 99}
    writes = []
    try:
        web.MATCH_QUEUE_LOCK = threading.RLock()
        web._read_match_queue_unlocked = lambda: [foreign]
        web._match_queue_scope = lambda: (7, False)
        web._match_queue_item_visible = lambda item, uid, _legacy: item.get("user_id") == uid
        web._match_queue_stamp = lambda item, uid: {**item, "user_id": uid}
        web._queue_item_from_suggestion = lambda suggestion: {
            "bridge_key": suggestion["bridge_key"],
            "ebook_filename": suggestion["matches"][0]["ebook_filename"],
        }
        web._write_match_queue_unlocked = lambda items: writes.append(copy.deepcopy(items))

        response = app.test_client().post(
            "/api/suggestions/selection/queue",
            json={"generation": "scan-1", "suggestion_keys": ["pair:abs-1"]},
        )
        assert response.status_code == 200
        payload = response.get_json()
        assert payload["counts"]["added"] == 1
        assert payload["added_keys"] == ["abs-1"]
        assert len(writes) == 1
        assert writes[0][0] == foreign
        assert writes[0][1]["user_id"] == 7
        assert writes[0][1]["bridge_key"] == "abs-1"
    finally:
        _close(patches)


def test_ebook_ignore_and_reset_persist_current_users_record():
    app, web, _db, patches = _app_and_patches()
    payload = _pair_payload()
    entry = {
        "key": "ebook:path:%2Fbooks%2Fsolo.epub",
        "classification": "no_audio",
        "title": "Solo",
        "source": "Local File",
        "source_id": None,
        "path": "/books/solo.epub",
        "aliases": [{"source": "BookOrbit", "source_id": "secondary"}],
    }
    payload["ebook_discovery"] = {
        "generation": "ebook-1",
        "state": "complete",
        "entries": {entry["key"]: entry},
    }
    saves = []
    try:
        web.SUGGESTIONS_CACHE_LOCK = threading.RLock()
        web._load_persisted_suggestions_cache = lambda: copy.deepcopy(payload)
        web._save_persisted_suggestions_cache = lambda saved, **_kwargs: saves.append(copy.deepcopy(saved))

        client = app.test_client()
        ignored = client.post(
            "/api/suggestions/ebook-only/ignore",
            json={"mode": "ignore", "ebook_key": entry["key"], "generation": "ebook-1"},
        )
        assert ignored.status_code == 200
        saved_record = saves[-1]["ebook_ignored"][entry["key"]]["entry"]
        assert saved_record["aliases"] == entry["aliases"]
        assert saved_record["path"] == entry["path"]

        payload = saves[-1]
        reset = client.post(
            "/api/suggestions/ebook-only/ignore",
            json={"mode": "reset", "ebook_key": entry["key"], "generation": "ebook-1"},
        )
        assert reset.status_code == 200
        assert entry["key"] not in saves[-1]["ebook_ignored"]
        assert entry["key"] not in saves[-1]["ebook_discovery"]["entries"]
    finally:
        _close(patches)


def test_create_uses_server_cached_exact_local_path_and_rejects_client_path_fields(tmp_path):
    app, web, db, patches = _app_and_patches()
    exact_path = tmp_path / "selected.epub"
    exact_path.write_bytes(b"book")
    key = "ebook:id:local%20file:entry"
    entry = {
        "key": key,
        "classification": "no_audio",
        "title": "Selected",
        "source": "Local File",
        "source_id": "entry",
        "filename": "selected.epub",
        "path": str(exact_path),
        "aliases": [],
    }
    persisted = _pair_payload()
    persisted["ebook_discovery"] = {
        "generation": "ebook-1",
        "state": "complete",
        "entries": {key: entry},
    }
    calls = []
    try:
        web._load_persisted_suggestions_cache = lambda: persisted
        web._safe_local_source_path = lambda value: str(value) if str(value) == str(exact_path) else ""
        parser = SimpleNamespace(get_kosync_id=lambda path: calls.append((str(path), {})) or "kosync-hash")
        web.container = SimpleNamespace(ebook_parser=lambda: parser)
        db.get_book_by_kosync_id.return_value = None
        web._upsert_storyteller_mapping = lambda **kwargs: (
            calls.append(kwargs) or (MagicMock(abs_id="new-map"), None, None)
        )
        web._claim_book_for_user_id = lambda *_args: None

        bad = app.test_client().post(
            "/api/suggestions/ebook-only/create",
            json={
                "ebook_key": key,
                "generation": "ebook-1",
                "path": str(tmp_path / "attacker.epub"),
            },
        )
        assert bad.status_code == 400
        assert calls == []

        created = app.test_client().post(
            "/api/suggestions/ebook-only/create",
            json={"ebook_key": key, "generation": "ebook-1"},
        )
        assert created.status_code == 200
        assert calls[0][0] == str(exact_path)
        assert calls[1]["ebook_filename"] == str(exact_path)
        assert calls[1]["precomputed_kosync_doc_id"] == "kosync-hash"
    finally:
        _close(patches)


def test_create_reuses_real_mapping_validation_and_duplicate_never_claims_foreign_row(tmp_path, monkeypatch):
    from src.db.database_service import DatabaseService
    from src.db.models import Book

    monkeypatch.setenv("DB_JOURNAL_MODE", "DELETE")
    db = DatabaseService(str(tmp_path / "mapping.db"))
    user = db.create_user("selected-user", "password", role="admin")
    app, web, _mock_db, patches = _app_and_patches()
    selected = tmp_path / "edition.epub"
    selected.write_bytes(b"selected edition")
    entry = {
        "key": "ebook:selected", "classification": "no_audio", "source": "Local File",
        "source_id": None, "path": str(selected), "filename": selected.name, "title": "Selected",
    }
    saved = {"ebook_discovery": {"generation": "ebook-1", "state": "complete",
                                "entries": {entry["key"]: entry}}}
    try:
        monkeypatch.setattr(web, "database_service", db)
        web.get_current_user_id.return_value = user.id
        web._load_persisted_suggestions_cache = lambda: copy.deepcopy(saved)
        monkeypatch.setattr(web, "_safe_local_source_path", lambda path: str(path) if str(path) == str(selected) else "")
        monkeypatch.setattr(web, "container", SimpleNamespace(
            ebook_parser=lambda: SimpleNamespace(get_kosync_id=lambda path: "a" * 32),
        ))
        monkeypatch.setattr(web, "uc", lambda: SimpleNamespace(sync_clients={}))
        monkeypatch.setattr(web, "_enqueue_tracker_automatch", lambda *_args: None)
        monkeypatch.setattr(web, "_adopt_kosync_progress_for_book", lambda *_args: None)
        monkeypatch.setattr(web, "get_kosync_id_for_ebook",
                            MagicMock(side_effect=AssertionError("basename fallback")))

        db.save_book(Book(abs_id="legacy", ebook_filename="edition.epub"))
        legacy = app.test_client().post("/api/suggestions/ebook-only/create", json={
            "ebook_key": entry["key"], "generation": "ebook-1",
        })
        assert legacy.status_code == 409
        assert len(db.get_all_books()) == 1
        db.delete_book("legacy")

        db.save_book(Book(abs_id="ebook-" + "a" * 16, abs_title="Keep Target", kosync_doc_id="different"))
        collision = app.test_client().post("/api/suggestions/ebook-only/create", json={
            "ebook_key": entry["key"], "generation": "ebook-1",
        })
        assert collision.status_code == 409
        assert db.get_book("ebook-" + "a" * 16).abs_title == "Keep Target"
        db.delete_book("ebook-" + "a" * 16)

        response = app.test_client().post("/api/suggestions/ebook-only/create", json={
            "ebook_key": entry["key"], "generation": "ebook-1",
        })
        assert response.status_code == 200
        book = db.get_book(response.get_json()["abs_id"])
        assert book.sync_mode == "ebook_only"
        assert book.ebook_filename == str(selected)
        assert book.kosync_doc_id == "a" * 32
        assert [row.abs_id for row in db.get_all_books(user_id=user.id)] == [book.abs_id]

        duplicate = app.test_client().post("/api/suggestions/ebook-only/create", json={
            "ebook_key": entry["key"], "generation": "ebook-1",
        })
        assert duplicate.status_code == 409
        assert len(db.get_all_books()) == 1

        # A global existing hash is rejected even when that mapping is outside
        # this user's visible catalog. No automatic join or overwrite occurs.
        db.delete_book(book.abs_id)
        from src.utils.user_context import set_current_user_id, reset_current_user_id
        other = db.create_user("other-reader", "password")
        token = set_current_user_id(other.id)
        try:
            db.save_book(Book(abs_id="foreign", abs_title="Keep This", kosync_doc_id="a" * 32))
        finally:
            reset_current_user_id(token)
        foreign = app.test_client().post("/api/suggestions/ebook-only/create", json={
            "ebook_key": entry["key"], "generation": "ebook-1",
        })
        assert foreign.status_code == 409
        assert db.get_book("foreign").abs_title == "Keep This"
        assert db.get_all_books(user_id=user.id) == []
    finally:
        _close(patches)
        db.db_manager.close()


def test_bookorbit_create_resolves_one_file_without_filename_or_hash_fallback(tmp_path, monkeypatch):
    from src import web_server as web
    client = MagicMock()
    client.get_book_detail.return_value = {"files": [{"id": 0, "filename": "exact.cbz"}]}
    client._primary_file.side_effect = lambda detail, kind: detail["files"][0]
    seen = []

    def download(file_id, path):
        seen.append((file_id, str(path)))
        path.write_bytes(b"exact bytes")
        return True

    client.download_file_to_path.side_effect = download
    parser = SimpleNamespace(get_kosync_id=lambda path: (
        seen.append(("hash", path.suffix)) or "selected-hash"
    ))
    monkeypatch.setattr(web, "container", SimpleNamespace(ebook_parser=lambda: parser))
    monkeypatch.setattr(web, "uc", lambda: SimpleNamespace(bookorbit_client=client))
    entry = {"source": "BookOrbit", "source_id": "book", "source_file_id": 0, "title": "Exact"}
    resolved = web._resolve_suggestions_redesign_ebook_create(entry)
    assert resolved["ebook_filename"] == "exact.cbz"
    assert resolved["precomputed_kosync_doc_id"] == "selected-hash"
    assert seen[0][0] == 0
    assert seen[1] == ("hash", ".cbz")
    client.download_book.assert_not_called()

    entry["source_file_id"] = "changed"
    assert web._resolve_suggestions_redesign_ebook_create(entry) is None
    assert len(seen) == 2


def test_invalid_selection_fields_return_client_error():
    app, _web, _db, patches = _app_and_patches()
    try:
        for payload in (None, {"generation": "scan-1", "eligible": True}):
            response = app.test_client().post("/api/suggestions/selection/resolve", json=payload)
            assert response.status_code == 400
    finally:
        _close(patches)


def test_queue_persistence_failure_is_not_reported_as_added():
    app, web, _db, patches = _app_and_patches()
    try:
        web._read_match_queue_unlocked = lambda: []
        web._match_queue_scope = lambda: (7, False)
        web._match_queue_stamp = lambda item, uid: {**item, "user_id": uid}
        web._write_match_queue_unlocked = lambda _items: False
        response = app.test_client().post("/api/suggestions/selection/queue", json={
            "generation": "scan-1", "all_filtered": True,
        })
        assert response.status_code == 503
        assert response.get_json()["error"] == "persistence_failed"
    finally:
        _close(patches)


def test_routes_use_the_module_that_registered_them_not_a_fresh_import():
    """Production runs web_server as __main__: importing src.web_server there gave an
    uninitialized copy, so discovery failed with "NameError: name 'DATA_DIR' is not
    defined" and the admin-only list was updated on the wrong module."""
    from types import SimpleNamespace

    from src import web_server
    import src.api.suggestions_redesign as redesign

    saved = redesign._WEB_MODULE
    admin_before = set(web_server._ADMIN_ONLY_ENDPOINTS)
    running = SimpleNamespace(
        _ADMIN_ONLY_ENDPOINTS=set(),
        get_current_user_id=lambda: 7,
        current_user=lambda: None,
        _load_persisted_suggestions_cache=_pair_payload,
        database_service=SimpleNamespace(get_all_books=lambda user_id=None: []),
        _dashboard_visible_books_for_user=lambda books, _user: books,
        _load_match_queue=lambda: [],
        _get_ignored_suggestion_source_ids=lambda: [],
        _suggestion_bulk_block=lambda _s: None,
        _sanitize_cover_urls=lambda entries: entries,
        _running_suggestions_scan_job_id=lambda _uid: None,
        _get_suggestions_scan_job=lambda _job: None,
        logger=web_server.logger,
    )
    app = Flask(__name__)
    app.secret_key = "test"
    try:
        init_suggestions_redesign_routes(app, running)
        response = app.test_client().get("/api/suggestions/discovery?tab=pairs")
        assert response.status_code == 200, response.get_json()
        assert [e["suggestion_key"] for e in response.get_json()["entries"]] == ["pair:abs-1"]
        assert "suggestions_redesign_discovery" in running._ADMIN_ONLY_ENDPOINTS
        assert web_server._ADMIN_ONLY_ENDPOINTS == admin_before
    finally:
        redesign._WEB_MODULE = saved
        web_server._ADMIN_ONLY_ENDPOINTS.clear()
        web_server._ADMIN_ONLY_ENDPOINTS.update(admin_before)
