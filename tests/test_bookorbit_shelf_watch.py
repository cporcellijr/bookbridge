"""Tests that ShelfWatchService is correctly parameterized for BookOrbit."""

import os
import sys
from unittest.mock import MagicMock, patch

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.services.shelf_watch_service import ShelfWatchService


def _svc(source_name='BookOrbit', env_prefix='BOOKORBIT'):
    return ShelfWatchService(
        booklore_client=MagicMock(),
        database_service=MagicMock(),
        book_mapping_service=MagicMock(),
        source_name=source_name,
        env_prefix=env_prefix,
    )


def test_env_helpers_read_source_prefix():
    svc = _svc()
    with patch.dict(os.environ, {
        "BOOKORBIT_SHELF_WATCH_ENABLED": "true",
        "BOOKORBIT_SHELF_WATCH_NAME": "Reading Next",
        "BOOKORBIT_SHELF_NAME": "Synced",
        "BOOKORBIT_SHELF_WATCH_THRESHOLD": "88",
    }, clear=False):
        assert svc._is_enabled() is True
        assert svc._watch_shelf_name() == "Reading Next"
        assert svc._kobo_shelf_name() == "Synced"
        assert svc._threshold() == 88.0


def test_bookorbit_does_not_read_booklore_settings():
    svc = _svc()
    with patch.dict(os.environ, {
        "BOOKLORE_SHELF_WATCH_ENABLED": "true",
        "BOOKORBIT_SHELF_WATCH_ENABLED": "false",
    }, clear=False):
        assert svc._is_enabled() is False


def test_scan_key_namespaced_for_bookorbit():
    assert _svc()._scan_key("5") == "bookorbit:5"


def test_scan_key_bare_for_booklore():
    svc = _svc(source_name='BookLore', env_prefix='BOOKLORE')
    assert svc._scan_key("5") == "5"


def test_runs_in_global_cycle_uses_prefix():
    svc = _svc()
    with patch.dict(os.environ, {"BOOKORBIT_POLL_MODE": "custom"}, clear=False):
        assert svc.runs_in_global_cycle() is False
    with patch.dict(os.environ, {"BOOKORBIT_POLL_MODE": "global"}, clear=False):
        assert svc.runs_in_global_cycle() is True


def test_display_name_uses_bookorbit():
    assert _svc()._display_name() == "BookOrbit"


def test_display_name_maps_booklore_to_grimmory():
    svc = _svc(source_name='BookLore', env_prefix='BOOKLORE')
    assert svc._display_name() == "Grimmory"


def test_missing_shelf_warning_names_bookorbit(caplog):
    """The 'shelf does not exist' warning must name BookOrbit, not Grimmory,
    when the watcher is parameterized for BookOrbit (issue #288 follow-up)."""
    svc = _svc()
    svc.booklore_client.is_configured.return_value = True
    svc.booklore_client.list_books_on_shelf.return_value = []
    svc.booklore_client.get_all_shelves.return_value = [{"name": "Synced"}]
    with patch.dict(os.environ, {
        "BOOKORBIT_SHELF_WATCH_ENABLED": "true",
        "BOOKORBIT_SHELF_WATCH_NAME": "Up Next",
    }, clear=False):
        with caplog.at_level("WARNING"):
            svc.process_watch_shelf()
    msgs = [r.message for r in caplog.records]
    assert any("Up Next" in m and "BookOrbit UI" in m for m in msgs)
    assert not any("Grimmory" in m for m in msgs)


def test_pending_suggestion_carries_source_name():
    svc = _svc()
    captured = {}
    svc.database_service.save_pending_suggestion.side_effect = lambda s: captured.update(s=s)
    matches = [{
        "bridge_key": "abs-1", "audio_source": "ABS", "audio_source_id": "abs-1",
        "audio_title": "T", "audio_author": "A", "audio_cover_url": "", "score": 70,
    }]
    svc._create_pending_suggestion({"title": "Book"}, "Book.epub", "5", matches)
    import json
    meta = json.loads(captured["s"].origin_metadata_json)
    assert meta["source_name"] == "BookOrbit"
    assert meta["grimmory_id"] == "5"


# ---- #515 same-entry audio shortcut -------------------------------------

def _same_entry_svc(source_name='BookOrbit', env_prefix='BOOKORBIT', info=None, info_exc=None):
    suggestions = MagicMock()
    suggestions._scan_single_ebook.return_value = None
    bms = MagicMock()
    bms.create_audio_mapping_from_match.return_value = MagicMock(
        audio_source='BookOrbit', audio_source_id='77')
    svc = ShelfWatchService(
        booklore_client=MagicMock(), database_service=MagicMock(),
        book_mapping_service=bms, source_name=source_name, env_prefix=env_prefix,
        suggestions_service_factory=MagicMock(return_value=suggestions),
    )
    client = MagicMock()
    if info_exc:
        client.get_audiobook_info.side_effect = info_exc
    else:
        client.get_audiobook_info.return_value = info
    return svc, client, bms, suggestions


def _run_one(svc, client):
    book = {"id": "77", "title": "Some Book", "fileName": "some-book.epub"}
    return svc._process_one_book(book, "some-book.epub", "77", [], 95.0, "Up Next", "Kobo",
                                 active_client=client)


def test_same_entry_audio_auto_matches_when_pool_misses():
    svc, client, bms, sugg = _same_entry_svc(info={"tracks": [{"id": 1}], "duration_seconds": 3600.0})
    assert _run_one(svc, client) == 'auto_matched'
    kw = bms.create_audio_mapping_from_match.call_args.kwargs
    assert kw['audio_source'] == 'BookOrbit' and kw['audio_source_id'] == '77'
    assert kw['audio_provider_book_id'] == '77' and kw['audio_duration'] == 3600.0
    bms.create_ebook_only_mapping.assert_not_called()
    sugg._scan_single_ebook.assert_not_called()
    assert svc.database_service.upsert_shelf_watch_scan.call_args.kwargs['status'] == 'auto_matched'


def test_same_entry_without_audio_stays_ebook_only():
    for info in (None, {"tracks": []}):
        svc, client, bms, _ = _same_entry_svc(info=info)
        assert _run_one(svc, client) == 'ebook_only'
        bms.create_audio_mapping_from_match.assert_not_called()


def test_same_entry_check_error_falls_back_to_scan():
    svc, client, bms, sugg = _same_entry_svc(info_exc=RuntimeError("boom"))
    assert _run_one(svc, client) == 'ebook_only'
    sugg._scan_single_ebook.assert_called_once()
    bms.create_audio_mapping_from_match.assert_not_called()


def test_same_entry_not_attempted_for_grimmory():
    svc, client, bms, _ = _same_entry_svc(
        source_name='BookLore', env_prefix='BOOKLORE', info={"tracks": [{"id": 1}]})
    assert _run_one(svc, client) == 'ebook_only'
    client.get_audiobook_info.assert_not_called()
