"""Tests for the BookOrbit ebook + audio sync clients."""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.api.bookorbit_client import BookOrbitClient
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


def test_ebook_update_sends_koreader_xpointer_through_to_bookorbit():
    """The KOReader XPointer must survive the sync-client -> API-client hop (#415).

    BookOrbit hands `koreaderProgress` back to KOReader as the pull position; when
    it is missing BookOrbit derives a chapter-root xpointer from the CFI and the
    device opens at the top of the chapter instead of the synced position.
    """
    captured = {}
    with patch.dict(os.environ, {
        "BOOKORBIT_SERVER": "http://mock",
        "BOOKORBIT_USER": "u",
        "BOOKORBIT_PASSWORD": "p",
    }):
        client = BookOrbitClient()
    client.get_book_by_id = MagicMock(return_value={"id": 7, "ebookFileId": 12, "title": "X"})

    sc = BookOrbitSyncClient(client, ebook_parser=None)
    book = _book(ebook_source="BookOrbit", ebook_source_id="7", abs_id="abs1",
                 original_ebook_filename=None, ebook_filename="X.epub")
    req = UpdateProgressRequest(locator_result=LocatorResult(
        percentage=0.5,
        cfi="epubcfi(/6/24!/4/2/2)",
        perfect_ko_xpath="/body/DocFragment[12]/body/p[7]/text().0",
    ))

    with patch.object(
        client, "_make_request",
        side_effect=lambda m, e, p=None: captured.update(endpoint=e, payload=p)
        or MagicMock(status_code=204),
    ):
        res = sc.update_progress(book, req)

    assert res.success is True
    assert captured["endpoint"] == "/api/v1/books/files/12/progress"
    assert captured["payload"]["percentage"] == pytest.approx(50.0)
    assert captured["payload"]["cfi"] == "epubcfi(/6/24!/4/2/2)"
    assert captured["payload"]["koreaderProgress"] == "/body/DocFragment[12]/body/p[7]/text().0"


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


# ---- multi-file (track-per-chapter) semantics ----
# Verified against the BookOrbit player: positionSeconds is WITHIN the
# currentFileId track, percentage is whole-book. Mirrors a real library book
# ("A Children's Bible", 5 mp3 tracks).

_MULTI_TRACK_INFO = {
    "primary_file_id": 9378,
    "duration_seconds": 20049,
    "chapters": [],
    "tracks": [
        {"id": 9378, "filename": "t1.mp3", "format": "mp3", "duration_seconds": 3806},
        {"id": 9379, "filename": "t2.mp3", "format": "mp3", "duration_seconds": 4287},
        {"id": 9380, "filename": "t3.mp3", "format": "mp3", "duration_seconds": 4543},
        {"id": 9381, "filename": "t4.mp3", "format": "mp3", "duration_seconds": 2929},
        {"id": 9382, "filename": "t5.mp3", "format": "mp3", "duration_seconds": 4484},
    ],
}


def _multi_track_book():
    return _book(audio_source="BookOrbit", audio_source_id="4345", abs_id="bookorbit:4345",
                 audio_provider_book_id=None, audio_duration=20049, duration=20049,
                 transcript_file=None)


def test_audio_read_reconstructs_absolute_ts_from_track():
    client = MagicMock()
    client.get_audiobook_info.return_value = dict(_MULTI_TRACK_INFO)
    # 194s into track 2 (which starts at 3806s) => 4000s absolute
    client.get_audiobook_progress.return_value = {
        "pct": 0.1995, "position_seconds": 194.0, "current_file_id": 9379,
    }
    sc = BookOrbitAudioSyncClient(client, ebook_parser=None)
    state = sc.get_service_state(_multi_track_book(), prev_state=None)
    assert state.current["ts"] == pytest.approx(4000.0)
    assert state.current["pct"] == pytest.approx(0.1995)


def test_audio_read_reconstructs_absolute_ts_from_v2_asset():
    client = MagicMock()
    client.get_audiobook_info.return_value = {
        **_MULTI_TRACK_INFO,
        "playback_tracks": [
            {"id": "aud_a", "duration_seconds": 3806.0},
            {"id": "aud_b", "duration_seconds": 4287.0},
            {"id": "aud_c", "duration_seconds": 4472.0},
            {"id": "aud_d", "duration_seconds": 4484.0},
        ],
    }
    client.get_audiobook_progress.return_value = {
        "pct": 0.1995, "position_seconds": 194.0, "current_file_id": "aud_b",
    }
    sc = BookOrbitAudioSyncClient(client, ebook_parser=None)

    state = sc.get_service_state(_multi_track_book(), prev_state=None)

    assert state.current["ts"] == pytest.approx(4000.0)


def test_audio_read_unknown_file_id_falls_back_to_percentage():
    client = MagicMock()
    client.get_audiobook_info.return_value = dict(_MULTI_TRACK_INFO)
    client.get_audiobook_progress.return_value = {
        "pct": 0.5, "position_seconds": 100.0, "current_file_id": 99999,
    }
    sc = BookOrbitAudioSyncClient(client, ebook_parser=None)
    state = sc.get_service_state(_multi_track_book(), prev_state=None)
    # A within-track position on an unknown track is ambiguous — pct wins.
    assert state.current["ts"] == pytest.approx(0.5 * 20049)


def test_audio_write_targets_containing_track_not_primary():
    client = MagicMock()
    client.get_audiobook_info.return_value = dict(_MULTI_TRACK_INFO)
    client.update_audiobook_progress.return_value = True
    sc = BookOrbitAudioSyncClient(client, ebook_parser=None)
    # 50% of 20049s = 10024.5s absolute -> track 3 (starts 8093s, id 9380)
    req = UpdateProgressRequest(locator_result=LocatorResult(percentage=0.5))
    res = sc.update_progress(_multi_track_book(), req)
    assert res.success is True
    assert res.location == pytest.approx(10024.5)
    _, kwargs = client.update_audiobook_progress.call_args
    assert kwargs["current_file_id"] == 9380
    assert kwargs["position_seconds"] == pytest.approx(10024.5 - 8093.0)


def test_audio_write_past_end_clamps_to_last_track():
    client = MagicMock()
    client.get_audiobook_info.return_value = dict(_MULTI_TRACK_INFO)
    client.update_audiobook_progress.return_value = True
    sc = BookOrbitAudioSyncClient(client, ebook_parser=None)
    book = _multi_track_book()
    book.audio_duration = 30000  # stale/oversized duration on the Book row
    book.duration = 30000
    req = UpdateProgressRequest(locator_result=LocatorResult(percentage=1.0))
    res = sc.update_progress(book, req)
    assert res.success is True
    _, kwargs = client.update_audiobook_progress.call_args
    assert kwargs["current_file_id"] == 9382
    # clamped to the final track's duration
    assert kwargs["position_seconds"] <= 4484.0
