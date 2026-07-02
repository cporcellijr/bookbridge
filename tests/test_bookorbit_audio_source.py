"""Tests for BookOrbit-hosted audiobook support: client audio surface,
BookOrbitAudioSourceAdapter, forge staging, and sync_manager wiring."""

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

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
    "authors": [{"name": "Lydia Millet"}],
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


def test_search_audiobooks_filters_audio_hits_and_enriches():
    client = BookOrbitClient()
    client._search_raw = MagicMock(return_value=[
        {"id": 4345, "title": "A Children's Bible", "authors": ["Lydia Millet"], "formats": ["mp3"]},
        {"id": 2065, "title": "A Children's Bible", "authors": ["Lydia Millet"], "formats": ["epub"]},
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
        {"id": 4345, "title": "A Children's Bible", "authors": "Lydia Millet",
         "duration_seconds": 20049, "num_files": 5},
    ]
    adapter = BookOrbitAudioSourceAdapter(bo, tmp_path)
    results = adapter.search("bible")
    assert len(results) == 1
    r = results[0]
    assert isinstance(r, AudioResult)
    assert r.source == "BookOrbit"
    assert r.source_id == "4345"
    assert r.provider_book_id == "4345"
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


def test_bookorbit_session_logs_audio_leader_against_audio_book_id():
    bo = MagicMock()
    bo.is_configured.return_value = True
    sm = _sync_manager(bookorbit_client=bo)
    book = SimpleNamespace(
        audio_source="BookOrbit",
        audio_provider_book_id="4345",
        audio_source_id="4345",
        ebook_source=None,
        ebook_source_id=None,
        ebook_filename="x.epub",
        sync_mode="audiobook",
    )
    leader_state = SimpleNamespace(current={"pct": 0.5}, previous_pct=0.4)
    with patch.object(sm, "_compute_session_duration", return_value=120):
        sm._record_bookorbit_reading_session(book, "BookOrbitAudio", leader_state, {}, 1_000_000.0)
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
    leader_state = SimpleNamespace(current={"pct": 0.5}, previous_pct=0.4)
    with patch.object(sm, "_compute_session_duration", return_value=120):
        sm._record_bookorbit_reading_session(book, "KoSync", leader_state, {}, 1_000_000.0)
    bo.create_reading_session.assert_not_called()
