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
