"""Tests that ShelfWatchService is correctly parameterized for BookOrbit."""

import os
import sys
from types import SimpleNamespace
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


# ---- empty candidate pool still allows the same-entry shortcut ----------

def _run_shelf_empty_pool(info):
    svc, client, bms, sugg = _same_entry_svc(info=info)
    sugg._build_audiobook_candidate_pool.return_value = []
    client.is_configured.return_value = True
    client.list_books_on_shelf.return_value = [
        {"id": "77", "title": "Some Book", "fileName": "some-book.epub"}]
    svc._is_enabled = MagicMock(return_value=True)
    svc._watch_shelf_name = MagicMock(return_value="Up Next")
    svc._kobo_shelf_name = MagicMock(return_value="Kobo")
    svc._is_already_mapped = MagicMock(return_value=False)
    svc._is_throttled = MagicMock(return_value=False)
    stats = svc._process_watch_shelf(active_client=client)
    return svc, bms, sugg, stats


def test_empty_pool_same_entry_audio_still_auto_matches():
    svc, bms, sugg, stats = _run_shelf_empty_pool({"tracks": [{"id": 1}], "duration_seconds": 3600.0})
    assert stats['auto_matched'] == 1
    bms.create_audio_mapping_from_match.assert_called_once()
    bms.create_ebook_only_mapping.assert_not_called()
    sugg._scan_single_ebook.assert_not_called()


def test_empty_pool_without_same_entry_audio_stays_deferred():
    svc, bms, sugg, stats = _run_shelf_empty_pool({"tracks": []})
    assert stats['auto_matched'] == 0 and stats['ebook_only'] == 0
    bms.create_audio_mapping_from_match.assert_not_called()
    bms.create_ebook_only_mapping.assert_not_called()
    sugg._scan_single_ebook.assert_not_called()
    svc.database_service.upsert_shelf_watch_scan.assert_not_called()


# ---- #524 optional read-along for BookOrbit-audio auto-matches -----------


def _readalong_svc(*, source_name='BookOrbit', env_prefix='BOOKORBIT',
                   audio_source='BookOrbit', status='pending'):
    db = MagicMock()
    forge = MagicMock()
    mapping = MagicMock()
    saved = SimpleNamespace(abs_id='bookorbit:77', audio_source=audio_source,
                            audio_source_id='77', abs_title='Some Book', status=status)
    db.get_book.return_value = saved
    db.set_readalong_epub_requested.return_value = True
    mapping.create_audio_mapping_from_match.return_value = saved
    svc = ShelfWatchService(
        booklore_client=MagicMock(), database_service=db,
        book_mapping_service=mapping, source_name=source_name,
        env_prefix=env_prefix, forge_service=forge,
    )
    return svc, db, forge, mapping, saved


def _save_audio_match(svc, audio_source='BookOrbit'):
    svc._create_audio_mapping_and_move(
        {'title': 'Some Book'}, 'some-book.epub', '77',
        {'audio_source': audio_source, 'audio_source_id': '77',
         'audio_title': 'Some Book', 'score': 100},
        'Up Next', 'Kobo', active_client=MagicMock(),
    )


def test_readalong_enabled_bookorbit_audio_match_records_intent(caplog):
    svc, db, forge, _, _ = _readalong_svc()
    with patch.dict(os.environ, {'BOOKORBIT_SHELF_WATCH_READALONG': 'on'}):
        with caplog.at_level('INFO'):
            _save_audio_match(svc)
    db.set_readalong_epub_requested.assert_called_once_with('bookorbit:77', True)
    forge.generate_readalong_if_requested.assert_not_called()
    assert any("read-along requested for 'Some Book'" in rec.message for rec in caplog.records)


def test_readalong_disabled_does_not_record_intent():
    svc, db, forge, _, _ = _readalong_svc()
    with patch.dict(os.environ, {'BOOKORBIT_SHELF_WATCH_READALONG': 'false'}):
        _save_audio_match(svc)
    db.set_readalong_epub_requested.assert_not_called()
    forge.generate_readalong_if_requested.assert_not_called()


def test_readalong_reads_setting_again_for_every_match():
    svc, db, _, _, _ = _readalong_svc()
    with patch.dict(os.environ, {'BOOKORBIT_SHELF_WATCH_READALONG': 'false'}):
        _save_audio_match(svc)
    with patch.dict(os.environ, {'BOOKORBIT_SHELF_WATCH_READALONG': 'true'}):
        _save_audio_match(svc)
    db.set_readalong_epub_requested.assert_called_once_with('bookorbit:77', True)


def test_readalong_ignores_abs_audio_even_on_bookorbit_shelf():
    svc, db, forge, _, _ = _readalong_svc(audio_source='ABS')
    with patch.dict(os.environ, {'BOOKORBIT_SHELF_WATCH_READALONG': 'true'}):
        _save_audio_match(svc, 'ABS')
    db.set_readalong_epub_requested.assert_not_called()
    forge.generate_readalong_if_requested.assert_not_called()


def test_readalong_ebook_only_does_not_record_intent():
    svc, db, forge, mapping, _ = _readalong_svc()
    mapping.create_ebook_only_mapping.return_value = SimpleNamespace(abs_id='ebook-only')
    with patch.dict(os.environ, {'BOOKORBIT_SHELF_WATCH_READALONG': 'true'}):
        svc._create_ebook_only_and_move(
            {'title': 'Some Book'}, 'some-book.epub', '77',
            'Up Next', 'Kobo', active_client=MagicMock())
    db.set_readalong_epub_requested.assert_not_called()
    forge.generate_readalong_if_requested.assert_not_called()


def test_readalong_active_book_dispatches_immediately():
    svc, db, forge, _, saved = _readalong_svc(status='active')
    with patch.dict(os.environ, {'BOOKORBIT_SHELF_WATCH_READALONG': 'true'}):
        _save_audio_match(svc)
    db.set_readalong_epub_requested.assert_called_once_with('bookorbit:77', True)
    forge.generate_readalong_if_requested.assert_called_once_with(saved)


def test_readalong_grimmory_watcher_ignores_bookorbit_toggle():
    svc, db, forge, _, _ = _readalong_svc(
        source_name='BookLore', env_prefix='BOOKLORE')
    with patch.dict(os.environ, {'BOOKORBIT_SHELF_WATCH_READALONG': 'true'}):
        _save_audio_match(svc)
    db.set_readalong_epub_requested.assert_not_called()
    forge.generate_readalong_if_requested.assert_not_called()


def test_readalong_failed_mapping_save_does_not_record_intent():
    svc, db, forge, mapping, _ = _readalong_svc()
    mapping.create_audio_mapping_from_match.return_value = None
    with patch.dict(os.environ, {'BOOKORBIT_SHELF_WATCH_READALONG': 'true'}):
        _save_audio_match(svc)
    db.set_readalong_epub_requested.assert_not_called()
    forge.generate_readalong_if_requested.assert_not_called()


def test_readalong_intent_record_failure_does_not_block_shelf_move():
    svc, db, _, _, _ = _readalong_svc()
    db.set_readalong_epub_requested.side_effect = RuntimeError('DB unavailable')
    svc._move_shelf = MagicMock()
    with patch.dict(os.environ, {'BOOKORBIT_SHELF_WATCH_READALONG': 'true'}):
        _save_audio_match(svc)
    svc._move_shelf.assert_called_once()


def test_readalong_same_entry_auto_match_records_intent():
    svc, db, _, _, _ = _readalong_svc()
    client = MagicMock()
    client.get_audiobook_info.return_value = {'tracks': [{'id': 1}], 'duration_seconds': 3600}
    with patch.dict(os.environ, {'BOOKORBIT_SHELF_WATCH_READALONG': 'true'}):
        assert _run_one(svc, client) == 'auto_matched'
    db.set_readalong_epub_requested.assert_called_once_with('bookorbit:77', True)


def test_readalong_scored_audio_match_records_intent():
    svc, db, _, _, _ = _readalong_svc()
    client = MagicMock()
    client.get_audiobook_info.return_value = None
    suggestions = MagicMock()
    suggestions._scan_single_ebook.return_value = {'matches': [
        {'audio_source': 'BookOrbit', 'audio_source_id': '77',
         'audio_title': 'Some Book', 'score': 100},
    ]}
    svc.set_suggestions_service_factory(lambda: suggestions)
    with patch.dict(os.environ, {'BOOKORBIT_SHELF_WATCH_READALONG': 'true'}):
        assert _run_one(svc, client) == 'auto_matched'
    db.set_readalong_epub_requested.assert_called_once_with('bookorbit:77', True)
