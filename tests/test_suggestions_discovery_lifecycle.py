"""Regression checks for the combined forward/inverse scan and per-user storage."""

import copy
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

from src import web_server as web
from src.api.suggestions_redesign import init_suggestions_redesign_routes
from src.services.ebook_discovery import build_ebook_inventory
from src.services.suggestions_service import SuggestionsService


def _service():
    db = MagicMock()
    db.get_all_books.return_value = []
    service = SuggestionsService(
        database_service=db, container=MagicMock(), manager=MagicMock(),
        get_audiobooks_conditionally=lambda: [], get_searchable_ebooks=lambda _q: [],
        audiobook_matches_search=lambda *_a: False, get_abs_author=lambda _a: "",
        logger=MagicMock(),
    )
    service.scan_library_suggestions = MagicMock(return_value={
        "suggestions": [], "cache_by_abs": {}, "no_match_abs_ids": [],
        "stats": {"scanned_new": 0},
    })
    return service


def _ebook(source_id, title="A Singular Title"):
    return web.EbookResult(
        name=f"{source_id}.epub", title=title, authors="Ada Writer",
        source="Kavita", source_id=source_id,
    )


def _run_scan(ebooks, *, previous=None, audio=(), complete=True, full=False):
    service = _service()
    persisted = {
        **web._empty_suggestions_cache_payload(),
        **(previous or {}),
    }

    def load_ebooks(_query, **kwargs):
        kwargs["inventory_candidates"].extend(ebooks)
        web._record_suggestions_source_state(
            kwargs["source_states"], "Kavita", "ebook", True,
            complete=complete, count=len(ebooks),
        )
        return ebooks

    with patch.object(web, "_get_suggestions_service", return_value=service), \
            patch.object(web, "uc", return_value=SimpleNamespace()), \
            patch.object(web, "_load_persisted_suggestions_cache", return_value=persisted), \
            patch.object(web, "get_suggestion_audiobooks", return_value=list(audio)) as loader, \
            patch.object(web, "get_searchable_ebooks", side_effect=load_ebooks) as ebooks_loader:
        result = web.scan_library_suggestions(
            cached_suggestions_by_abs=persisted["scan_cache_by_abs"],
            cached_no_match_abs_ids=persisted["scan_cache_no_match_abs_ids"],
            full_refresh=full,
        )
    loader.assert_called_once()
    ebooks_loader.assert_called_once()
    assert ebooks_loader.call_args.kwargs["full_refresh"] is full
    return result, service


def test_scan_classifies_once_reuses_unchanged_and_full_refresh_rechecks():
    first, _ = _run_scan([_ebook("one")])
    assert first["discovery_complete"]
    assert first["ebook_discovery"]["stats"]["no_audio"] == 1
    previous = {
        "ebook_discovery": first["ebook_discovery"],
        "scan_generation": first["scan_generation"],
    }
    second, _ = _run_scan([_ebook("one")], previous=previous)
    assert second["ebook_discovery"]["stats"]["reused_cached"] == 1
    assert second["ebook_discovery"]["stats"]["candidate_count"] == 0
    full, _ = _run_scan([_ebook("one")], previous=previous, full=True)
    assert full["ebook_discovery"]["stats"]["scanned_new"] == 1
    assert full["ebook_discovery"]["stats"]["reused_cached"] == 0


@pytest.mark.parametrize("full", [False, True])
def test_incomplete_source_preserves_both_tabs_last_completed_generation(full):
    previous = {
        "ebook_discovery": {
            "generation": "reliable", "state": "complete", "completed_at": 100,
            "entries": {"saved": {"classification": "no_audio"}},
        },
        "scan_cache_by_abs": {"audio": {"bridge_key": "audio", "matches": []}},
        "scan_cache_no_match_abs_ids": ["other"],
        "scan_generation": "pairs", "scanned_at": 100,
    }
    result, service = _run_scan([_ebook("partial")], previous=previous, complete=False, full=full)
    snapshot = result["ebook_discovery"]
    assert not result["discovery_complete"]
    assert snapshot["state"] == "incomplete"
    assert snapshot["generation"] == "reliable"
    assert snapshot["completed_at"] == 100
    assert snapshot["entries"] == previous["ebook_discovery"]["entries"]
    assert snapshot["attempted_at"] > 100
    assert result["cache_by_abs"] == previous["scan_cache_by_abs"]
    assert result["scanned_at"] == 100
    service.scan_library_suggestions.assert_not_called()


def test_first_failed_source_never_publishes_a_false_no_match():
    result, _ = _run_scan([_ebook("partial")], complete=False)
    assert result["ebook_discovery"]["generation"] is None
    assert result["ebook_discovery"]["entries"] == {}
    assert result["ebook_discovery"]["state"] == "incomplete"


def test_deleted_inventory_prunes_and_persistent_ignore_is_applied_on_full_refresh():
    first, _ = _run_scan([_ebook("removed"), _ebook("ignored")])
    ignored_entry = next(row for row in first["ebook_discovery"]["entries"].values() if row["source_id"] == "ignored")
    result, _ = _run_scan([_ebook("ignored")], full=True, previous={
        "ebook_discovery": first["ebook_discovery"],
        "ebook_ignored": {ignored_entry["key"]: {"entry": ignored_entry}},
    })
    assert result["ebook_discovery"]["entries"] == {}
    assert result["ebook_discovery"]["stats"]["ignored"] == 1


def test_forward_review_candidate_is_excluded_from_ebook_only_by_identity():
    inventory = build_ebook_inventory([_ebook("same"), _ebook("other")])
    snapshot = {
        "entries": {key: {**row, "classification": "no_audio", "audio_keys": []} for key, row in inventory.items()},
        "stats": {},
    }
    web._reconcile_discovery_with_pair_matches(snapshot, {
        "audio": {"matches": [{"source": "Kavita", "source_id": "same", "score": 55}]},
    })
    by_id = {row["source_id"]: row for row in snapshot["entries"].values()}
    assert by_id["same"]["classification"] == "has_audio"
    assert by_id["same"]["audio_keys"] == ["audio"]
    assert by_id["other"]["classification"] == "no_audio"


def test_changed_ebook_invalidates_only_related_pair_anchors_including_review_shortlist():
    previous = {
        "audio_fingerprints_by_key": {"a": "same", "b": "same"},
        "entries": {"ebook": {"fingerprint": "old", "audio_keys": ["a"]}},
    }
    current = {
        "audio_fingerprints_by_key": {"a": "same", "b": "same"},
        "entries": {"ebook": {"fingerprint": "new", "candidate_audio_keys": ["a"]}},
    }
    cache, no_match, count = web._invalidate_suggestions_pair_cache(
        {"a": {"matches": []}, "b": {"matches": []}}, ["a", "b"], previous, current,
    )
    assert set(cache) == {"b"}
    assert no_match == ["b"]
    assert count == 1


def test_persistence_is_user_scoped_and_a_scan_cannot_undo_a_concurrent_ignore(tmp_path):
    entry = next(iter(build_ebook_inventory([_ebook("one")]).values()))
    snapshot = {"state": "complete", "generation": "g1", "entries": {entry["key"]: entry}}
    with patch.object(web, "DATA_DIR", tmp_path, create=True):
        with patch.object(web, "get_current_user_id", return_value=1):
            assert web._save_persisted_suggestions_cache({
                "ebook_discovery": snapshot, "ebook_ignored": {entry["key"]: {"entry": entry}},
            }, replace_ignored=True)
            assert web._save_persisted_suggestions_cache({
                "ebook_discovery": copy.deepcopy(snapshot), "ebook_ignored": {},
                "scan_cache_by_abs": {"audio": {}},
            })
            saved = web._load_persisted_suggestions_cache()
            assert entry["key"] in saved["ebook_ignored"]
            assert saved["ebook_discovery"]["entries"] == {}
            # Legacy pair-only writers preserve inverse metadata and ignores.
            assert web._save_persisted_suggestions_cache({"scan_last_stats": {"scanned_new": 1}})
            assert web._load_persisted_suggestions_cache()["ebook_discovery"]["generation"] == "g1"
        with patch.object(web, "get_current_user_id", return_value=2):
            other = web._load_persisted_suggestions_cache()
            assert other["ebook_ignored"] == {}
            assert other["scan_cache_by_abs"] == {}


def test_ignore_reset_survives_real_json_round_trip_and_requires_rescan(tmp_path):
    app = Flask(__name__)
    init_suggestions_redesign_routes(app)
    entry = next(iter(build_ebook_inventory([_ebook("one")]).values()))
    entry["classification"] = "no_audio"
    with patch.object(web, "DATA_DIR", tmp_path, create=True), patch.object(web, "get_current_user_id", return_value=7):
        web._save_persisted_suggestions_cache({
            "ebook_discovery": {"state": "complete", "generation": "g1", "entries": {entry["key"]: entry}},
        })
        for mode in ("ignore", "reset"):
            response = app.test_client().post("/api/suggestions/ebook-only/ignore", json={
                "mode": mode, "ebook_key": entry["key"], "generation": "g1",
            })
            assert response.status_code == 200
            saved = web._load_persisted_suggestions_cache()
            assert (entry["key"] in saved["ebook_ignored"]) == (mode == "ignore")
            assert saved["ebook_discovery"]["entries"] == {}


def test_failed_cache_write_is_visible_to_ignore_action(tmp_path):
    app = Flask(__name__)
    init_suggestions_redesign_routes(app)
    entry = next(iter(build_ebook_inventory([_ebook("one")]).values()))
    entry["classification"] = "no_audio"
    persisted = {"ebook_discovery": {"generation": "g1", "entries": {entry["key"]: entry}}}
    with patch.object(web, "get_current_user_id", return_value=7), \
            patch.object(web, "_load_persisted_suggestions_cache", return_value=persisted), \
            patch.object(web, "_save_persisted_suggestions_cache", return_value=False):
        response = app.test_client().post("/api/suggestions/ebook-only/ignore", json={
            "mode": "ignore", "ebook_key": entry["key"], "generation": "g1",
        })
        assert response.status_code == 503
        assert response.get_json()["error"] == "persistence_failed"


def test_add_book_preselects_saved_identity_and_uses_only_targeted_audio_search(monkeypatch):
    app = Flask(__name__)
    app.secret_key = "test"
    entry = next(iter(build_ebook_inventory([_ebook("selected")]).values()))
    entry["classification"] = "no_audio"
    monkeypatch.setattr(web, "uc", lambda: SimpleNamespace(
        storyteller_client=SimpleNamespace(is_configured=lambda: False),
    ))
    monkeypatch.setattr(web, "current_user", lambda: SimpleNamespace(id=7, is_admin=True))
    monkeypatch.setattr(web, "get_current_user_id", lambda: 7)
    monkeypatch.setattr(web, "database_service", SimpleNamespace(get_all_books=lambda **_kw: []))
    monkeypatch.setattr(web, "_load_match_queue", lambda: [])
    monkeypatch.setattr(web, "_load_persisted_suggestions_cache", lambda: {
        "ebook_discovery": {"generation": "g1", "state": "complete", "entries": {entry["key"]: entry}},
    })
    audio = MagicMock(return_value=[])
    monkeypatch.setattr(web, "_search_audiobooks_with_fallback", audio)
    monkeypatch.setattr(web, "_search_ebooks_with_fallback",
                        MagicMock(side_effect=AssertionError("full ebook search")))
    monkeypatch.setattr(web, "render_template", lambda _name, **kwargs: kwargs)
    with app.test_request_context("/add-book", query_string={"ebook_key": entry["key"], "generation": "g1"}):
        result = web._add_book_view()
    assert len(result["ebooks"]) == 1
    selected = result["ebooks"][0]
    assert (selected.source, selected.source_id) == ("Kavita", "selected")
    audio.assert_called_once_with("A Singular Title")
