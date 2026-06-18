"""Tests for the BookOrbit ebook + audio sync clients."""

import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.sync_clients.bookorbit_sync_client import BookOrbitSyncClient
from src.sync_clients.bookorbit_audio_sync_client import BookOrbitAudioSyncClient
from src.sync_clients.sync_client_interface import LocatorResult, UpdateProgressRequest


def _book(**kw):
    b = MagicMock()
    for k, v in kw.items():
        setattr(b, k, v)
    return b


# ---- ebook sync client ----

def test_ebook_supports_book_by_source():
    client = MagicMock()
    sc = BookOrbitSyncClient(client, ebook_parser=None)
    assert sc.supports_book(_book(ebook_source="BookOrbit")) is True


def test_ebook_resolves_by_source_id_fast_path():
    client = MagicMock()
    client.get_book_by_id.return_value = {"id": 7, "title": "X"}
    client.get_ebook_progress.return_value = (0.4, "cfi1")
    sc = BookOrbitSyncClient(client, ebook_parser=None)
    book = _book(ebook_source="BookOrbit", ebook_source_id="7",
                 original_ebook_filename=None, ebook_filename="X.epub")
    state = sc.get_service_state(book, prev_state=None)
    assert state is not None
    assert state.current["pct"] == 0.4
    client.get_book_by_id.assert_called_once_with(7)


def test_ebook_update_records_write():
    client = MagicMock()
    client.get_book_by_id.return_value = {"id": 7}
    client.update_ebook_progress.return_value = True
    sc = BookOrbitSyncClient(client, ebook_parser=None)
    book = _book(ebook_source="BookOrbit", ebook_source_id="7", abs_id="abs1",
                 original_ebook_filename=None, ebook_filename="X.epub")
    req = UpdateProgressRequest(locator_result=LocatorResult(percentage=0.5, cfi="cfiZ"))
    res = sc.update_progress(book, req)
    assert res.success is True
    assert res.updated_state["pct"] == 0.5
    assert res.updated_state["cfi"] == "cfiZ"


# ---- audio sync client ----

def test_audio_supports_only_bookorbit_source():
    sc = BookOrbitAudioSyncClient(MagicMock(), ebook_parser=None)
    assert sc.supports_book(_book(audio_source="BookOrbit")) is True
    assert sc.supports_book(_book(audio_source="ABS")) is False


def test_audio_get_service_state_uses_position_seconds():
    client = MagicMock()
    client.get_audiobook_progress.return_value = {"pct": 0.25, "position_seconds": 3600.0, "current_file_id": 11}
    sc = BookOrbitAudioSyncClient(client, ebook_parser=None)
    book = _book(audio_source="BookOrbit", audio_source_id="5",
                 audio_provider_book_id=None, audio_duration=14400, duration=14400)
    state = sc.get_service_state(book, prev_state=None)
    assert state.current["ts"] == 3600.0
    assert state.current["pct"] == 0.25


def test_audio_update_resolves_file_id_and_writes():
    client = MagicMock()
    client.get_audiobook_info.return_value = {"primary_file_id": 11, "duration_seconds": 14400}
    client.update_audiobook_progress.return_value = True
    sc = BookOrbitAudioSyncClient(client, ebook_parser=None)
    book = _book(audio_source="BookOrbit", audio_source_id="5", abs_id="abs1",
                 audio_provider_book_id=None, audio_duration=14400, duration=14400,
                 transcript_file=None)
    req = UpdateProgressRequest(locator_result=LocatorResult(percentage=0.5))
    res = sc.update_progress(book, req)
    assert res.success is True
    # 50% of 14400s = 7200s
    assert res.location == pytest.approx(7200.0)
    _, kwargs = client.update_audiobook_progress.call_args
    assert kwargs["current_file_id"] == 11
    assert kwargs["position_seconds"] == pytest.approx(7200.0)
