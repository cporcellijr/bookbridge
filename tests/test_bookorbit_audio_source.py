"""Tests for BookOrbit-hosted audiobook support: client audio surface,
BookOrbitAudioSourceAdapter, forge staging, and sync_manager wiring."""

import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.api.api_clients import ABSClient
from src.api.booklore_client import BookloreClient
from src.api.bookorbit_client import BookOrbitClient
from src.services.audio_source_adapters import AudioResult, BookOrbitAudioSourceAdapter
from src.services.forge_service import ForgeService
from src.sync_manager import SyncManager


# ---------------------------------------------------------------------------
# BookOrbitClient audio surface
# ---------------------------------------------------------------------------

_DETAIL_MULTI = {
    "id": 4345,
    "title": "A Children's Bible",
    "authors": [{"name": "Nora Whitfield"}],
    "audioMetadata": {
        "durationSeconds": 20049,
        "chapters": [
            {"title": "t1", "startMs": 0},
            {"title": "t2", "startMs": 3806000},
        ],
    },
    "files": [
        {"id": 9378, "format": "mp3", "role": "primary", "filename": "t1.mp3",
         "durationSeconds": 3806, "sizeBytes": 100, "absolutePath": "/books/x/t1.mp3"},
        {"id": 9500, "format": "jpg", "role": "cover", "filename": "cover.jpg"},
        {"id": 9379, "format": "mp3", "role": "content", "filename": "t2.mp3",
         "durationSeconds": 4287, "sizeBytes": 200, "absolutePath": "/books/x/t2.mp3"},
    ],
}


def _client_with_detail(detail):
    client = BookOrbitClient()
    client.get_book_detail = MagicMock(return_value=detail)
    return client


def test_get_audiobook_info_lists_tracks_in_detail_order():
    info = _client_with_detail(_DETAIL_MULTI).get_audiobook_info(4345)
    assert [t["id"] for t in info["tracks"]] == [9378, 9379]  # cover jpg skipped
    assert info["tracks"][0]["duration_seconds"] == 3806
    assert info["tracks"][1]["absolute_path"] == "/books/x/t2.mp3"
    assert info["primary_file_id"] == 9378
    assert info["duration_seconds"] == 20049


def test_get_audiobook_info_duration_falls_back_to_track_sum():
    detail = {
        "id": 1,
        "files": [
            {"id": 1, "format": "mp3", "role": "primary", "durationSeconds": 10},
            {"id": 2, "format": "mp3", "role": "content", "durationSeconds": 20},
        ],
    }
    info = _client_with_detail(detail).get_audiobook_info(1)
    assert info["duration_seconds"] == 30


def test_get_audiobook_info_uses_v2_manifest_for_playback_timeline():
    client = _client_with_detail(_DETAIL_MULTI)
    client._audiobook_api = "playback"
    client._get_audiobook_manifest = MagicMock(return_value={
        "revision": "b" * 64,
        "totalDurationMs": 20049000,
        "assets": [
            {"assetId": "aud_a", "sequence": 0, "durationMs": 3806000},
            {"assetId": "aud_b", "sequence": 1, "durationMs": 4287000},
        ],
        "chapters": [{"title": "Opening", "startMs": 0}],
    })

    info = client.get_audiobook_info(4345)

    assert [track["id"] for track in info["tracks"]] == [9378, 9379]
    assert [track["id"] for track in info["playback_tracks"]] == ["aud_a", "aud_b"]
    assert info["primary_playback_id"] == "aud_a"
    assert info["duration_seconds"] == 20049
    assert info["chapters"] == [{"title": "Opening", "startMs": 0}]


def test_search_audiobooks_filters_audio_hits_and_enriches():
    client = BookOrbitClient()
    client._search_raw = MagicMock(return_value=[
        {"id": 4345, "title": "A Children's Bible", "authors": ["Nora Whitfield"], "formats": ["mp3"]},
        {"id": 2065, "title": "A Children's Bible", "authors": ["Nora Whitfield"], "formats": ["epub"]},
    ])
    client.get_audiobook_info = MagicMock(return_value={
        "duration_seconds": 20049,
        "tracks": [{"size_bytes": 100}, {"size_bytes": 200}],
    })
    results = client.search_audiobooks("children's bible")
    assert len(results) == 1
    assert results[0]["id"] == 4345
    assert results[0]["duration_seconds"] == 20049
    assert results[0]["num_files"] == 2
    assert results[0]["total_size_bytes"] == 300


def test_search_audiobooks_empty_query_uses_cache_without_detail_calls():
    client = BookOrbitClient()
    client._book_cache = {
        1: {"id": 1, "title": "Audio", "authors": "A", "kind": "audiobook"},
        2: {"id": 2, "title": "Ebook", "authors": "B", "kind": "ebook"},
    }
    client._cache_timestamp = 9e12  # keep _ensure_cache from refreshing
    client.get_audiobook_info = MagicMock()
    results = client.search_audiobooks("")
    assert [r["id"] for r in results] == [1]
    client.get_audiobook_info.assert_not_called()


# ---------------------------------------------------------------------------
# BookOrbitAudioSourceAdapter
# ---------------------------------------------------------------------------

def test_adapter_search_maps_audio_results(tmp_path):
    bo = MagicMock()
    bo.search_audiobooks.return_value = [
        {"id": 4345, "title": "A Children's Bible", "authors": "Nora Whitfield",
         "language": "en", "duration_seconds": 20049, "num_files": 5},
    ]
    adapter = BookOrbitAudioSourceAdapter(bo, tmp_path)
    results = adapter.search("bible")
    assert len(results) == 1
    r = results[0]
    assert isinstance(r, AudioResult)
    assert r.source == "BookOrbit"
    assert r.source_id == "4345"
    assert r.provider_book_id == "4345"
    assert r.language == "en"
    assert r.duration == pytest.approx(20049)
    assert r.cover_url == "/api/bookorbit/audiobook-cover/4345"


def test_adapter_chapters_from_markers(tmp_path):
    bo = MagicMock()
    bo.get_audiobook_info.return_value = {
        "duration_seconds": 100.0,
        "chapters": [
            {"title": "One", "startMs": 0},
            {"title": "Two", "startMs": 40000},
        ],
        "tracks": [],
    }
    adapter = BookOrbitAudioSourceAdapter(bo, tmp_path)
    chapters = adapter.get_chapters("1")
    assert chapters == [
        {"id": 0, "title": "One", "start": 0.0, "end": 40.0},
        {"id": 1, "title": "Two", "start": 40.0, "end": 100.0},
    ]


def test_adapter_chapters_fall_back_to_tracks(tmp_path):
    bo = MagicMock()
    bo.get_audiobook_info.return_value = {
        "duration_seconds": 30.0,
        "chapters": [],
        "tracks": [
            {"id": 1, "filename": "part1.mp3", "duration_seconds": 10.0},
            {"id": 2, "filename": "part2.mp3", "duration_seconds": 20.0},
        ],
    }
    adapter = BookOrbitAudioSourceAdapter(bo, tmp_path)
    chapters = adapter.get_chapters("1")
    assert [c["title"] for c in chapters] == ["part1", "part2"]
    assert chapters[1]["start"] == pytest.approx(10.0)
    assert chapters[1]["end"] == pytest.approx(30.0)


def test_adapter_get_audio_files_downloads_and_caches(tmp_path):
    bo = MagicMock()
    bo.get_audiobook_info.return_value = {
        "tracks": [
            {"id": 11, "format": "mp3", "duration_seconds": 10.0},
            {"id": 12, "format": "mp3", "duration_seconds": 20.0},
        ],
    }

    def fake_download(file_id, local_path):
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        Path(local_path).write_bytes(b"audio")
        return True

    bo.download_file_to_path.side_effect = fake_download
    adapter = BookOrbitAudioSourceAdapter(bo, tmp_path)

    files = adapter.get_audio_files("7", bridge_key="bookorbit:7")
    assert len(files) == 2
    assert bo.download_file_to_path.call_count == 2
    assert files[0]["local_path"].endswith("track_000.mp3")
    assert files[1]["duration_ms"] == 20000
    # bridge key is sanitized for Windows-safe cache dirs ('bookorbit:7' -> 'bookorbit_7')
    assert "bookorbit_7" in files[0]["local_path"]

    # Second call reuses the cached files — no new downloads.
    bo.download_file_to_path.reset_mock()
    files_again = adapter.get_audio_files("7", bridge_key="bookorbit:7")
    assert len(files_again) == 2
    bo.download_file_to_path.assert_not_called()


def test_adapter_get_audio_files_raises_on_failed_download(tmp_path):
    bo = MagicMock()
    bo.get_audiobook_info.return_value = {"tracks": [{"id": 11, "format": "mp3"}]}
    bo.download_file_to_path.return_value = False
    adapter = BookOrbitAudioSourceAdapter(bo, tmp_path)
    with pytest.raises(RuntimeError):
        adapter.get_audio_files("7")


@pytest.fixture
def streaming_client():
    client = BookOrbitClient()
    client._get_fresh_token = MagicMock(return_value="test-token")
    response = MagicMock()
    response.status_code = 200
    response.headers = {"Content-Length": "12"}
    response.__enter__.return_value = response
    client.session.get = MagicMock(return_value=response)
    return client, response


def test_mob_sorcery_partial_cache_recovers_on_retry(tmp_path, streaming_client, caplog):
    """A nonempty partial M4B must not poison every subsequent mapping attempt."""
    client, response = streaming_client
    client.get_audiobook_info = MagicMock(return_value={"tracks": [{
        "id": 11, "format": "m4b", "duration_seconds": 74412, "size_bytes": 12,
    }]})
    adapter = BookOrbitAudioSourceAdapter(client, tmp_path)
    cached = tmp_path / "audio_cache/bookorbit_5542/source_tracks/track_000.m4b"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"partial")

    def interrupted():
        yield b"part"
        raise ConnectionError("download interrupted")

    response.iter_content.side_effect = lambda **_: interrupted()
    with pytest.raises(RuntimeError, match="BookOrbit track download failed"):
        adapter.get_audio_files("5542", bridge_key="bookorbit:5542")
    assert cached.read_bytes() == b"partial"
    assert list(cached.parent.glob("*.part")) == []

    response.iter_content.side_effect = lambda **_: iter([b"full", b" audio!!"])
    files = adapter.get_audio_files("5542", bridge_key="bookorbit:5542")
    assert Path(files[0]["local_path"]).read_bytes() == b"full audio!!"
    assert "cached=7 expected=12; re-downloading" in caplog.text
    client.session.get.reset_mock()
    adapter.get_audio_files("5542", bridge_key="bookorbit:5542")
    client.session.get.assert_not_called()


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("failure", ["interrupted", "short", "empty"])
def test_download_never_publishes_incomplete_file(tmp_path, streaming_client, existing, failure):
    client, response = streaming_client
    target = tmp_path / "track.m4b"
    if existing:
        target.write_bytes(b"original")

    def chunks():
        if failure != "empty":
            yield b"partial"
        if failure == "interrupted":
            raise ConnectionError("download interrupted")

    response.iter_content.side_effect = lambda **_: chunks()
    assert client.download_file_to_path(11, target) is False
    assert target.read_bytes() == b"original" if existing else not target.exists()
    assert list(tmp_path.glob("*.part")) == []


@pytest.mark.parametrize("headers", [{"Content-Length": "12"}, {}, {"Content-Length": "unknown"}])
def test_download_publishes_complete_file_atomically(tmp_path, streaming_client, headers):
    client, response = streaming_client
    response.headers = headers
    target = tmp_path / "track.m4b"
    target.write_bytes(b"original")

    def chunks():
        yield b"full"
        assert target.read_bytes() == b"original"
        yield b" audio!!"
        assert target.read_bytes() == b"original"

    response.iter_content.side_effect = lambda **_: chunks()
    assert client.download_file_to_path(11, target) is True
    assert target.read_bytes() == b"full audio!!"
    assert list(tmp_path.glob("*.part")) == []


def test_adapter_rejects_short_download_without_content_length(tmp_path, streaming_client):
    client, response = streaming_client
    response.headers = {}
    response.iter_content.return_value = [b"partial"]
    client.get_audiobook_info = MagicMock(return_value={"tracks": [{
        "id": 11, "format": "m4b", "size_bytes": 12,
    }]})
    adapter = BookOrbitAudioSourceAdapter(client, tmp_path)
    with pytest.raises(RuntimeError, match="got 7 bytes, expected 12"):
        adapter.get_audio_files("5542", bridge_key="bookorbit:5542")
    assert not (tmp_path / "audio_cache/bookorbit_5542/source_tracks/track_000.m4b").exists()


def test_abs_download_file_preserves_existing_file_on_incomplete_response(tmp_path):
    client = ABSClient()
    client.session = MagicMock()
    response = MagicMock()
    response.status_code = 200
    response.headers = {"Content-Length": "12"}
    response.__enter__.return_value = response
    response.iter_content.return_value = [b"partial"]
    client.session.get.return_value = response

    target = tmp_path / "track.mp3"
    target.write_bytes(b"original")

    assert client.download_file("http://example.test/file.mp3", str(target)) is False
    assert target.read_bytes() == b"original"
    assert list(tmp_path.glob("*.part")) == []


def test_booklore_track_download_preserves_existing_file_on_incomplete_response(tmp_path):
    client = BookloreClient(database_service=MagicMock())
    client._get_fresh_token = MagicMock(return_value="token")
    response = MagicMock()
    response.status_code = 200
    response.headers = {"Content-Length": "12"}
    response.__enter__.return_value = response
    response.iter_content.return_value = [b"partial"]
    client.session.get = MagicMock(return_value=response)

    target = tmp_path / "track.mp3"
    target.write_bytes(b"original")

    assert client.download_audiobook_track("book-1", 0, str(target)) is False
    assert target.read_bytes() == b"original"
    assert list(tmp_path.glob("*.part")) == []


def _encoded_response(payload):
    """A gzip response: Content-Length is the wire size, iter_content is decoded."""
    response = MagicMock()
    response.status_code = 200
    response.headers = {"Content-Length": "12", "Content-Encoding": "gzip"}
    response.__enter__.return_value = response
    response.iter_content.return_value = [payload]
    return response


def test_abs_download_file_accepts_a_transparently_decoded_body(tmp_path):
    """Content-Length counts compressed bytes, so it must not reject a good download."""
    client = ABSClient()
    client.session = MagicMock()
    client.session.get.return_value = _encoded_response(b"A" * 5000)

    target = tmp_path / "book.epub"
    assert client.download_file("http://example.test/book.epub", str(target)) is True
    assert target.stat().st_size == 5000


def test_booklore_track_download_accepts_a_transparently_decoded_body(tmp_path):
    client = BookloreClient(database_service=MagicMock())
    client._get_fresh_token = MagicMock(return_value="token")
    client.session.get = MagicMock(return_value=_encoded_response(b"A" * 5000))

    target = tmp_path / "track.mp3"
    assert client.download_audiobook_track("book-1", 0, str(target)) is True
    assert target.stat().st_size == 5000


def test_abs_download_file_reports_truncation_with_byte_counts(caplog):
    """The truncation diagnostic issue reporters paste back must still fire."""
    client = ABSClient()
    client.session = MagicMock()
    response = MagicMock()
    response.status_code = 200
    response.headers = {"Content-Length": "12"}
    response.__enter__.return_value = response
    response.iter_content.return_value = [b"partial"]
    client.session.get.return_value = response

    with tempfile.TemporaryDirectory() as tmp:
        with caplog.at_level(logging.ERROR):
            assert client.download_file("http://example.test/f.mp3", str(Path(tmp) / "f.mp3")) is False

    assert "❌ ABS Download truncated: got 7 bytes, expected 12" in caplog.text


def test_abs_download_file_rejects_an_error_page_without_clobbering_the_cache(tmp_path):
    """A complete but 1 KiB body is an error page; the cached file must survive."""
    client = ABSClient()
    client.session = MagicMock()
    response = MagicMock()
    response.status_code = 200
    response.headers = {"Content-Length": "100"}
    response.__enter__.return_value = response
    response.iter_content.return_value = [b"B" * 100]
    client.session.get.return_value = response

    target = tmp_path / "book.epub"
    target.write_bytes(b"a previously downloaded epub")

    assert client.download_file("http://example.test/book.epub", str(target)) is False
    assert target.read_bytes() == b"a previously downloaded epub"


def test_booklore_whole_file_download_keeps_previous_file_when_stream_truncates(tmp_path):
    """A rejected candidate endpoint must not leave a partial audiobook behind."""
    client = BookloreClient(database_service=MagicMock())
    client._get_fresh_token = MagicMock(return_value="token")
    response = MagicMock()
    response.status_code = 200
    response.headers = {"Content-Length": "5000"}
    response.__enter__.return_value = response
    response.iter_content.return_value = [b"partial"]
    client.session.get = MagicMock(return_value=response)

    target = tmp_path / "book.m4b"
    target.write_bytes(b"a previously downloaded audiobook")

    assert client.download_book_to_path("book-1", str(target), expected_size=5000) is False
    assert target.read_bytes() == b"a previously downloaded audiobook"
    assert list(tmp_path.glob("*.part")) == []


# ---------------------------------------------------------------------------
# ForgeService staging + Whisper inputs
# ---------------------------------------------------------------------------

def _forge_service(bookorbit_client, ebook_parser=None):
    return ForgeService(
        database_service=MagicMock(),
        abs_client=MagicMock(),
        booklore_client=MagicMock(),
        storyteller_client=MagicMock(),
        library_service=MagicMock(),
        ebook_parser=ebook_parser or MagicMock(),
        transcriber=MagicMock(),
        alignment_service=MagicMock(),
        bookorbit_client=bookorbit_client,
    )


def test_copy_bookorbit_audio_files_downloads_tracks(tmp_path):
    bo = MagicMock()
    bo.get_audiobook_info.return_value = {
        "tracks": [
            {"id": 11, "format": "mp3", "absolute_path": None},
            {"id": 12, "format": "m4b", "absolute_path": "/nonexistent/x.m4b"},
        ],
    }

    def fake_download(file_id, dest):
        Path(dest).write_bytes(b"audio")
        return True

    bo.download_file_to_path.side_effect = fake_download
    svc = _forge_service(bo)
    assert svc._copy_bookorbit_audio_files("7", tmp_path) is True
    assert (tmp_path / "track_000.mp3").exists()
    assert (tmp_path / "track_001.m4b").exists()


def test_copy_bookorbit_audio_files_stages_local_shared_mount(tmp_path):
    src_file = tmp_path / "src" / "book.m4b"
    src_file.parent.mkdir()
    src_file.write_bytes(b"local audio")
    dest = tmp_path / "stage"

    bo = MagicMock()
    bo.get_audiobook_info.return_value = {
        "tracks": [{"id": 11, "format": "m4b", "absolute_path": str(src_file)}],
    }
    svc = _forge_service(bo)
    assert svc._copy_bookorbit_audio_files("7", dest) is True
    assert (dest / "track_000.m4b").read_bytes() == b"local audio"
    bo.download_file_to_path.assert_not_called()


def test_copy_bookorbit_audio_files_fails_when_download_fails(tmp_path):
    bo = MagicMock()
    bo.get_audiobook_info.return_value = {"tracks": [{"id": 11, "format": "mp3"}]}
    bo.download_file_to_path.return_value = False
    svc = _forge_service(bo)
    assert svc._copy_bookorbit_audio_files("7", tmp_path) is False


def test_whisper_inputs_use_namespaced_bookorbit_cache(tmp_path):
    bo = MagicMock()
    parser = MagicMock()
    parser.epub_cache_dir = tmp_path
    svc = _forge_service(bo, ebook_parser=parser)

    def fake_copy(book_id, cache_root, stage_mode=None):
        Path(cache_root).mkdir(parents=True, exist_ok=True)
        (Path(cache_root) / "track_000.mp3").write_bytes(b"audio")
        return True

    with patch.object(svc, "_copy_bookorbit_audio_files", side_effect=fake_copy) as copy_mock:
        inputs = svc._get_whisper_audio_inputs(tmp_path / "empty", "bookorbit:7", "BookOrbit", "7")

    assert len(inputs) == 1
    cache_root = Path(copy_mock.call_args[0][1])
    assert cache_root.name == "bookorbit_7"  # namespaced, no Grimmory id collision


# ---------------------------------------------------------------------------
# SyncManager wiring
# ---------------------------------------------------------------------------

def _sync_manager(**kw):
    kw.setdefault("sync_clients", {})
    kw.setdefault("database_service", MagicMock())
    return SyncManager(**kw)


def test_primary_audio_client_name_for_bookorbit():
    sm = _sync_manager()
    book = SimpleNamespace(audio_source="BookOrbit", sync_mode="audiobook")
    assert sm._get_primary_audio_client_name(book) == "BookOrbitAudio"


def test_bundle_adapters_include_bookorbit():
    sm = _sync_manager(data_dir=Path("/tmp"))
    bundle = SimpleNamespace(
        abs_client=MagicMock(),
        booklore_client=MagicMock(),
        bookorbit_client=MagicMock(),
    )
    with patch.object(SyncManager, "active_client_bundle", new_callable=PropertyMock, return_value=bundle):
        adapters = sm.active_audio_source_adapters
    assert "BookOrbit" in adapters
    assert isinstance(adapters["BookOrbit"], BookOrbitAudioSourceAdapter)



def _buffer_row(book_id, candidates, session_type="EPUB", end_location=None):
    """A closed buffer row as the delivery pass sees it."""
    return SimpleNamespace(
        id=1, abs_id="bookorbit:4345", session_type=session_type,
        leader_client="BookOrbitAudio", start_progress=0.4, end_progress=0.5,
        last_event_at=1_000_000.0, end_location=end_location,
        bookorbit_book_id=book_id, bookorbit_candidate_ids=json.dumps(candidates),
    )

def test_bookorbit_session_logs_audio_leader_against_audio_book_id():
    bo = MagicMock()
    bo.is_configured.return_value = True
    bo.find_covering_sessions.return_value = []  # BookOrbit has not logged it (#424)
    bo.create_reading_session.return_value = True
    sm = _sync_manager(bookorbit_client=bo)
    sm.database_service = MagicMock()
    book = SimpleNamespace(
        audio_source="BookOrbit",
        audio_provider_book_id="4345",
        audio_source_id="4345",
        ebook_source=None,
        ebook_source_id=None,
        ebook_filename="x.epub",
        sync_mode="audiobook",
    )
    book_id, candidates = sm._resolve_bookorbit_session_ids(book, audio=True)
    assert book_id == 4345
    assert sm._deliver_reading_session(
        _buffer_row(book_id, candidates, session_type="AUDIOBOOK"), "bookorbit", 120,
    )
    _, kwargs = bo.create_reading_session.call_args
    assert kwargs["book_id"] == 4345
    assert kwargs["book_type"] == "AUDIOBOOK"


def test_bookorbit_session_skips_ebook_leader_without_bookorbit_ebook():
    bo = MagicMock()
    bo.is_configured.return_value = True
    sm = _sync_manager(bookorbit_client=bo)
    book = SimpleNamespace(
        audio_source="BookOrbit",
        audio_provider_book_id="4345",
        audio_source_id="4345",
        ebook_source="BookLore",
        ebook_source_id="99",
        ebook_filename="x.epub",
        sync_mode="audiobook",
    )
    # An ebook-leader session is never logged against the audiobook.
    book_id, _ = sm._resolve_bookorbit_session_ids(book, audio=False)
    assert book_id is None
    bo.create_reading_session.assert_not_called()
