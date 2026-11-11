"""Regression tests for the Suggestions UI code-review fixes."""

import copy
import os
import re
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import src.web_server as web_server
from src.api.bookorbit_client import BookOrbitClient
from tests.test_suggestions_redesign_api import (  # noqa: F401 (autouse fixture restores web globals)
    _app_and_patches, _close, restore_web_helpers,
)

_TEMPLATE = Path(__file__).resolve().parents[1] / "templates" / "suggestions.html"
HASH = "a" * 32
KEY = "ebook:selected"


def _create_env(tmp_path, monkeypatch):
    """Real SQLite + bare Flask app wired for POST /api/suggestions/ebook-only/create."""
    from src.db.database_service import DatabaseService

    monkeypatch.setenv("DB_JOURNAL_MODE", "DELETE")
    db = DatabaseService(str(tmp_path / "fixes.db"))
    user = db.create_user("selected-user", "password", role="admin")
    app, web, _mock, patches = _app_and_patches()
    selected = tmp_path / "edition.epub"
    selected.write_bytes(b"selected edition")
    entry = {"key": KEY, "classification": "no_audio", "source": "Local File", "source_id": None,
             "path": str(selected), "filename": selected.name, "title": "Selected"}
    saved = {"ebook_discovery": {"generation": "ebook-1", "state": "complete", "entries": {KEY: entry}}}
    monkeypatch.setattr(web, "database_service", db)
    web.get_current_user_id.return_value = user.id
    web._load_persisted_suggestions_cache = lambda: copy.deepcopy(saved)
    monkeypatch.setattr(web, "_safe_local_source_path", lambda p: str(p) if str(p) == str(selected) else "")
    monkeypatch.setattr(web, "container", SimpleNamespace(
        ebook_parser=lambda: SimpleNamespace(get_kosync_id=lambda path: HASH)))
    monkeypatch.setattr(web, "uc", lambda: SimpleNamespace(sync_clients={}))
    monkeypatch.setattr(web, "_enqueue_tracker_automatch", lambda *_a: None)
    monkeypatch.setattr(web, "_adopt_kosync_progress_for_book", lambda *_a: None)
    return app, web, db, patches


def _create(app):
    return app.test_client().post("/api/suggestions/ebook-only/create",
                                  json={"ebook_key": KEY, "generation": "ebook-1"})


def test_save_book_create_only_leaves_existing_row_untouched(tmp_path, monkeypatch):
    from src.db.database_service import DatabaseService
    from src.db.models import Book

    monkeypatch.setenv("DB_JOURNAL_MODE", "DELETE")
    db = DatabaseService(str(tmp_path / "sb.db"))
    try:
        db.save_book(Book(abs_id="x", ebook_filename="old.epub", kosync_doc_id="oldhash"))
        result = db.save_book(Book(abs_id="x", ebook_filename="new.epub", kosync_doc_id="newhash"),
                              create_only=True)
        assert result is None
        row = db.get_book("x")
        assert (row.ebook_filename, row.kosync_doc_id) == ("old.epub", "oldhash")
        assert db.save_book(Book(abs_id="y", ebook_filename="y.epub"), create_only=True).abs_id == "y"
    finally:
        db.db_manager.close()


def test_create_race_row_inserted_after_precheck_returns_409_and_is_untouched(tmp_path, monkeypatch):
    from src.db.models import Book

    app, web, db, patches = _create_env(tmp_path, monkeypatch)
    try:
        target = "ebook-" + HASH[:16]
        db.save_book(Book(abs_id=target, abs_title="Other", ebook_filename="other-edition.epub",
                          kosync_doc_id="otherhash"))
        real_get_book = db.get_book
        # Every pre-check sees no occupant; only the insert-time guard can stop the overwrite.
        monkeypatch.setattr(db, "get_book", lambda *_a, **_k: None)
        response = _create(app)
        assert response.status_code == 409
        assert response.get_json()["error"] == "already_mapped"
        row = real_get_book(target)
        assert (row.ebook_filename, row.kosync_doc_id) == ("other-edition.epub", "otherhash")
    finally:
        _close(patches)
        db.db_manager.close()


def test_create_rejects_hash_already_linked_via_kosync_document(tmp_path, monkeypatch):
    from src.db.models import Book, KosyncDocument

    app, web, db, patches = _create_env(tmp_path, monkeypatch)
    try:
        db.save_book(Book(abs_id="foreign", abs_title="Foreign"))
        db.save_kosync_document(KosyncDocument(document_hash=HASH, linked_abs_id="foreign"))
        response = _create(app)
        assert response.status_code == 409
        assert response.get_json()["error"] == "already_mapped"
        assert [b.abs_id for b in db.get_all_books()] == ["foreign"]
    finally:
        _close(patches)
        db.db_manager.close()


class TestScanUsesStrictClients(unittest.TestCase):
    def _run(self, is_primary):
        global_abs = MagicMock()
        global_abs.is_configured.return_value = True
        user = SimpleNamespace(id=7, is_admin=True)
        db = MagicMock()
        db.get_user.return_value = user
        db.is_primary_admin.return_value = is_primary
        container = MagicMock()
        container.user_client_registry.return_value.get_clients.side_effect = RuntimeError("no bundle")
        saved = web_server._global_clients
        try:
            web_server._global_clients = web_server._GlobalClients()
            with patch.object(web_server, "database_service", db), \
                 patch.object(web_server, "container", container), \
                 patch.object(web_server, "get_current_user_id", return_value=7), \
                 patch.object(web_server, "current_user", return_value=user), \
                 patch.object(web_server, "_load_persisted_suggestions_cache", return_value={}), \
                 patch.object(web_server, "_get_suggestions_service", return_value=MagicMock()), \
                 patch.object(web_server, "get_suggestion_audiobooks", return_value=[]) as audio, \
                 patch.object(web_server, "get_searchable_ebooks", return_value=[]), \
                 patch.object(web_server._GlobalClients, "abs_client", global_abs, create=True):
                try:
                    result = web_server.scan_library_suggestions()
                except Exception as exc:  # the allowed path continues into the real pipeline
                    result = exc
                return result, global_abs, audio
        finally:
            web_server._global_clients = saved

    def test_non_primary_admin_never_reads_global_catalogs(self):
        result, global_abs, audio = self._run(is_primary=False)
        self.assertFalse(result["discovery_complete"])
        self.assertEqual(result["source_states"]["_catalog_capture"]["roles"]["ebook"]["error_type"], "PermissionError")
        global_abs.get_library_catalog.assert_not_called()
        audio.assert_not_called()

    def test_primary_admin_may_use_global_clients(self):
        result, global_abs, audio = self._run(is_primary=True)
        self.assertTrue(audio.called)
        states = result.get("source_states", {}) if isinstance(result, dict) else {}
        self.assertNotIn("_catalog_capture", states)


class TestStaleBulkGeneration(unittest.TestCase):
    def setUp(self):
        from tests.test_match_paths_regression import TestMatchPathsRegression as Base
        self.base = Base("setUp")
        self.base.setUp()
        self.addCleanup(self.base.tearDown)
        self.client = self.base.client
        token = web_server.set_current_user_id(7)
        self.addCleanup(web_server.reset_current_user_id, token)
        web_server._match_queue_clear()
        evidence = {"author_agrees": True, "source": "BookOrbit", "score": 100.0}
        web_server._save_persisted_suggestions_cache({
            "scan_generation": "g2",
            "scan_cache_by_abs": {"ok": {
                "bridge_key": "ok", "abs_id": "ok", "audio_source": "ABS", "audio_source_id": "ok",
                "audio_title": "ok", "audio_duration": 3600, "audio_cover_url": "",
                "matches": [dict(evidence, ebook_filename="ok.epub", display_name="Ok",
                                 source_id="1", title_key="ok")]}},
            "scan_cache_no_match_abs_ids": [], "scan_last_stats": {}, "updated_at": time.time(),
        })

    def _post(self, **extra):
        data = {"action": "add_many_to_queue", "bulk_intent": "exact", "bridge_keys": ["ok"], **extra}
        return self.client.post("/suggestions", data=data, headers={"X-Requested-With": "XMLHttpRequest"})

    def _queued(self):
        return {i["bridge_key"] for i in web_server._load_match_queue()}

    def test_stale_generation_is_refused_and_queue_stays_empty(self):
        response = self._post(generation="g1")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json(), {"error": "stale_generation"})
        self.assertEqual(self._queued(), set())

    def test_current_generation_queues(self):
        self.assertEqual(self._post(generation="g2").status_code, 200)
        self.assertEqual(self._queued(), {"ok"})

    def test_missing_generation_stays_backward_compatible(self):
        self.assertEqual(self._post().status_code, 200)
        self.assertEqual(self._queued(), {"ok"})


class _Resp:
    def __init__(self, payload=None, status_code=200):
        self._payload, self.status_code, self.content = payload, status_code, b""

    def json(self):
        return self._payload


@pytest.fixture
def bo_client():
    with patch.dict(os.environ, {"BOOKORBIT_SERVER": "http://mock", "BOOKORBIT_USER": "u",
                                 "BOOKORBIT_PASSWORD": "p"}):
        yield BookOrbitClient()


@pytest.mark.parametrize("failure", [None, _Resp({}, 500)])
def test_fresh_only_detail_does_not_fall_back_to_cache(bo_client, failure):
    bo_client._detail_cache["b1"] = (time.time() - 10_000, {"id": "b1", "old": True})
    with patch.object(bo_client, "_make_request", return_value=failure):
        assert bo_client.get_book_detail("b1", force=True, fresh_only=True) is None
        assert bo_client.get_book_detail("b1", force=True) == {"id": "b1", "old": True}


def test_suggestions_ebook_result_rejects_cached_detail_when_fresh_read_fails(bo_client):
    bo_client._detail_cache["b1"] = (time.time(), {"id": "b1", "files": [
        {"id": 1, "filename": "old-file.epub", "format": "epub", "role": "primary"}]})
    with patch.object(bo_client, "_make_request", return_value=None), \
         patch.object(web_server, "uc", return_value=SimpleNamespace(bookorbit_client=bo_client)):
        entry = {"source": "BookOrbit", "source_id": "b1", "title": "T"}
        assert web_server._suggestions_ebook_result(entry) is None
        # Control: the same cached detail resolves when the fresh read is allowed to fall back.
        assert bo_client.get_book_detail("b1", force=True)["files"][0]["filename"] == "old-file.epub"


def _src() -> str:
    return _TEMPLATE.read_text(encoding="utf-8")


def test_template_load_view_rethrows_abort_error_when_reading_body():
    src = _src()
    body = src[src.index("async function loadView("):]
    body = body[:body.index("\n    }\n")]
    assert "response.json().catch(() => null)" not in body
    assert re.search(r"response\.json\(\)\.catch\(\(err\) => \{ if \(err\.name === 'AbortError'\) throw err;", body)


def test_template_add_many_sends_generation_and_handles_409():
    src = _src()
    body = src[src.index("action', 'add_many_to_queue'"):]
    body = body[:body.index("queueList")]
    assert "formData.append('generation'" in body
    assert "response.status === 409)" in body


def test_create_only_loses_a_concurrent_insert_race_without_overwriting(tmp_path):
    """Both creates read "absent", both insert; the loser must not update the winner's row."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from sqlalchemy.orm import Query

    from src.db.database_service import DatabaseService
    from src.db.models import Book

    db = DatabaseService(str(tmp_path / "create-race.db"))
    try:
        both_absent = threading.Barrier(2)
        real_first = Query.first
        read_once = threading.local()

        def first(query):
            result = real_first(query)
            if query.column_descriptions[0].get("entity") is Book and not getattr(read_once, "done", False):
                read_once.done = True
                assert result is None
                both_absent.wait(timeout=5)
            return result

        def create(filename):
            return db.save_book(Book(abs_id="ebook-race", abs_title="Race", ebook_filename=filename,
                                     kosync_doc_id=f"hash-{filename}", sync_mode="ebook_only"), create_only=True)

        with patch.object(Query, "first", first), ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(create, ["first.epub", "second.epub"]))
        winners = [result for result in results if result is not None]
        assert len(winners) == 1, results
        stored = db.get_book("ebook-race")
        assert stored.ebook_filename == winners[0].ebook_filename
        assert stored.kosync_doc_id == winners[0].kosync_doc_id
    finally:
        db.db_manager.close()
