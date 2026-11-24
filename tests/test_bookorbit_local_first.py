"""Local-first BookOrbit audio reuse must never modify source files."""

from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.services.audio_source_adapters import BookOrbitAudioSourceAdapter
from src.sync_manager import SyncManager


def _track(path: Path, ident: int, duration: float = 11.25, **overrides):
    metadata = {
        "id": ident,
        "filename": path.name,
        "absolute_path": str(path),
        "size_bytes": path.stat().st_size,
        "format": path.suffix.lstrip("."),
        "duration_seconds": duration,
    }
    metadata.update(overrides)
    return metadata


def _copy_on_download(source: Path):
    def downloader(_file_id, destination):
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())
        return True
    return downloader


def test_mounted_multiple_tracks_keep_provider_order_and_require_no_cache(tmp_path, monkeypatch):
    root = tmp_path / "audiobooks"
    root.mkdir()
    first = root / "B.m4b"
    second = root / "A.mp3"
    first.write_bytes(b"first audio data")
    second.write_bytes(b"second source bytes")
    originals = [first.read_bytes(), second.read_bytes()]
    monkeypatch.setenv("AUDIOBOOKS_DIR", str(root))
    client = MagicMock()
    client.get_audiobook_info.return_value = {
        "tracks": [_track(first, 51, 10.5), _track(second, 52, 12.75)]
    }
    adapter = BookOrbitAudioSourceAdapter(client, tmp_path)
    files = adapter.get_audio_files("9", bridge_key="bookorbit:9")
    assert [Path(f["local_path"]) for f in files] == [first, second]
    assert [f["track_index"] for f in files] == [0, 1]
    assert [f["duration_ms"] for f in files] == [10500, 12750]
    assert [f["ext"] for f in files] == ["m4b", "mp3"]
    assert SyncManager._ctc_local_audio_paths(None, adapter, "9", "bookorbit:9") == [str(first), str(second)]
    client.download_file_to_path.assert_not_called()
    assert not (tmp_path / "audio_cache").exists()
    assert [first.read_bytes(), second.read_bytes()] == originals


@pytest.mark.parametrize("bad", [
    "missing_id", "missing_size", "wrong_size", "renamed", "relative_path",
    "traversal", "outside_mount", "missing_path", "missing_root",
])
def test_invalid_metadata_or_mount_falls_back_to_download(tmp_path, monkeypatch, bad):
    root = tmp_path / "audiobooks"
    root.mkdir()
    original = root / "track.m4b"
    original.write_bytes(b"source bytes")
    outside = tmp_path / "outside.m4b"
    outside.write_bytes(b"source bytes")
    monkeypatch.setenv("AUDIOBOOKS_DIR", str(root if bad != "missing_root" else tmp_path / "missing"))
    metadata = _track(original, 17)
    if bad == "missing_id": metadata["id"] = None
    if bad == "missing_size": metadata.pop("size_bytes")
    if bad == "wrong_size": metadata["size_bytes"] += 5
    if bad == "renamed": metadata["filename"] = "different.m4b"
    if bad == "relative_path": metadata["absolute_path"] = "track.m4b"
    if bad == "traversal": metadata["absolute_path"] = str(root / "sub" / ".." / original.name)
    if bad == "outside_mount": metadata["absolute_path"] = str(outside)
    if bad == "missing_path": metadata["absolute_path"] = None
    client = MagicMock()
    client.get_audiobook_info.return_value = {"tracks": [metadata]}
    # If the provider size is wrong or absent, the existing download validation
    # must be allowed to reject or accept only the expected complete bytes.
    copy_bytes = b"source bytes" if bad != "wrong_size" else b"a" * metadata["size_bytes"]

    def download(_id, destination):
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(copy_bytes)
        return True

    client.download_file_to_path.side_effect = download
    adapter = BookOrbitAudioSourceAdapter(client, tmp_path)
    files = adapter.get_audio_files("10", bridge_key="bookorbit:10")
    cached = Path(files[0]["local_path"])
    assert cached.parent.name == "source_tracks"
    assert cached.read_bytes() == copy_bytes
    client.download_file_to_path.assert_called_once()
    assert original.read_bytes() == b"source bytes"


def test_symlink_component_rejected_even_without_windows_symlink_privilege(tmp_path, monkeypatch):
    root = tmp_path / "audiobooks"
    root.mkdir()
    source = root / "file.mp3"
    source.write_bytes(b"mounted bytes")
    monkeypatch.setenv("AUDIOBOOKS_DIR", str(root))
    client = MagicMock()
    client.get_audiobook_info.return_value = {"tracks": [_track(source, 77)]}
    client.download_file_to_path.side_effect = _copy_on_download(source)
    original_is_symlink = Path.is_symlink

    def symlink_check(candidate):
        if candidate == source:
            return True
        return original_is_symlink(candidate)

    with patch.object(Path, "is_symlink", autospec=True, side_effect=symlink_check):
        files = BookOrbitAudioSourceAdapter(client, tmp_path).get_audio_files("77")
    assert Path(files[0]["local_path"]).parent.name == "source_tracks"
    assert source.read_bytes() == b"mounted bytes"
    client.download_file_to_path.assert_called_once()


def test_symlink_escape_falls_back(tmp_path, monkeypatch):
    root = tmp_path / "audiobooks"
    root.mkdir()
    outside = tmp_path / "outside.mp3"
    outside.write_bytes(b"outside original")
    fake_link = root / "track.mp3"
    try:
        fake_link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable under Windows permissions")
    monkeypatch.setenv("AUDIOBOOKS_DIR", str(root))
    track = _track(fake_link, 11)
    client = MagicMock()
    client.get_audiobook_info.return_value = {"tracks": [track]}
    client.download_file_to_path.side_effect = _copy_on_download(outside)
    path = BookOrbitAudioSourceAdapter(client, tmp_path).get_audio_files("11")[0]["local_path"]
    assert Path(path).parent.name == "source_tracks"
    client.download_file_to_path.assert_called_once()
    assert outside.read_bytes() == b"outside original"


def test_duplicate_filenames_use_authoritative_paths_not_name_lookup(tmp_path, monkeypatch):
    root = tmp_path / "audiobooks"
    a = root / "book-a" / "chapter.mp3"
    b = root / "book-b" / "chapter.mp3"
    a.parent.mkdir(parents=True)
    b.parent.mkdir(parents=True)
    a.write_bytes(b"different a")
    b.write_bytes(b"different b")
    monkeypatch.setenv("AUDIOBOOKS_DIR", str(root))
    client = MagicMock()
    client.get_audiobook_info.return_value = {"tracks": [_track(b, 72), _track(a, 71)]}
    files = BookOrbitAudioSourceAdapter(client, tmp_path).get_audio_files("book")
    assert [Path(f["local_path"]) for f in files] == [b, a]
    client.download_file_to_path.assert_not_called()


def test_changed_source_is_revalidated_and_falls_back(tmp_path, monkeypatch):
    root = tmp_path / "audiobooks"
    root.mkdir()
    original = root / "chapter.m4b"
    original.write_bytes(b"original")
    monkeypatch.setenv("AUDIOBOOKS_DIR", str(root))
    client = MagicMock()
    client.get_audiobook_info.return_value = {"tracks": [_track(original, 18)]}
    def download_expected(_id, destination):
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"original")
        return True

    client.download_file_to_path.side_effect = download_expected
    adapter = BookOrbitAudioSourceAdapter(client, tmp_path)
    assert Path(adapter.get_audio_files("18")[0]["local_path"]) == original
    original.write_bytes(b"now changed size")
    files = adapter.get_audio_files("18")
    cached = Path(files[0]["local_path"])
    assert cached.parent.name == "source_tracks"
    assert cached.read_bytes() == b"original"
    assert original.read_bytes() == b"now changed size"
    client.download_file_to_path.assert_called_once()


def test_parallel_local_jobs_do_not_download_or_write_source(tmp_path, monkeypatch):
    root = tmp_path / "audiobooks"
    root.mkdir()
    original = root / "one.m4b"
    original.write_bytes(b"concurrent use of original")
    before = sha256(original.read_bytes()).hexdigest()
    monkeypatch.setenv("AUDIOBOOKS_DIR", str(root))
    client = MagicMock()
    client.get_audiobook_info.return_value = {"tracks": [_track(original, 19)]}
    adapter = BookOrbitAudioSourceAdapter(client, tmp_path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: adapter.get_audio_files("19"), range(8)))
    assert all(Path(r[0]["local_path"]) == original for r in results)
    assert sha256(original.read_bytes()).hexdigest() == before
    client.download_file_to_path.assert_not_called()
    assert not (tmp_path / "audio_cache").exists()


def test_local_and_downloaded_tracks_have_identical_bytes_order_and_durations(tmp_path, monkeypatch):
    root = tmp_path / "audiobooks"
    root.mkdir()
    paths = [root / "01.mp3", root / "02.m4b"]
    paths[0].write_bytes(b"AAA" * 100)
    paths[1].write_bytes(b"BBB" * 200)
    tracks = [_track(paths[0], 31, 9.25), _track(paths[1], 32, 17.5)]
    client = MagicMock()
    client.get_audiobook_info.return_value = {"tracks": tracks}
    monkeypatch.setenv("AUDIOBOOKS_DIR", str(root))
    adapter = BookOrbitAudioSourceAdapter(client, tmp_path)
    local = adapter.get_audio_files("31", bridge_key="bookorbit:31")
    assert [f["local_path"] for f in local] == [str(p) for p in paths]
    monkeypatch.setenv("AUDIOBOOKS_DIR", str(tmp_path / "absent"))

    def download(file_id, destination):
        p=paths[file_id - 31]
        target=Path(destination)
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes(p.read_bytes())
        return True

    client.download_file_to_path.side_effect = download
    cached = adapter.get_audio_files("31", bridge_key="bookorbit:31")
    assert [Path(x["local_path"]).read_bytes() for x in cached] == [p.read_bytes() for p in paths]
    assert [(x["track_index"],x["duration_ms"],x["ext"]) for x in local] == [
        (x["track_index"],x["duration_ms"],x["ext"]) for x in cached
    ]
    assert client.download_file_to_path.call_count == 2


@pytest.mark.parametrize("unsafe_key", [
    "../escape", "a/../../escape", r"..\escape", "/outside",
    "bookorbit:17/../../escape",
])
def test_cache_key_traversal_is_rejected_before_download(tmp_path, monkeypatch, unsafe_key):
    root = tmp_path / "audiobooks"
    root.mkdir()
    original = root / "a.mp3"
    original.write_bytes(b"real source")
    monkeypatch.setenv("AUDIOBOOKS_DIR", str(root))
    client = MagicMock()
    client.get_audiobook_info.return_value = {"tracks": [_track(original, 17)]}
    with pytest.raises(ValueError, match="Unsafe BookOrbit audio cache"):
        BookOrbitAudioSourceAdapter(client, tmp_path).get_audio_files(
            "17", bridge_key=unsafe_key)
    client.download_file_to_path.assert_not_called()
    assert not (tmp_path / "escape").exists()


def test_symlinked_audio_cache_source_tracks_is_rejected(tmp_path, monkeypatch):
    root = tmp_path / "audiobooks"
    root.mkdir()
    original = root / "a.mp3"
    original.write_bytes(b"real source")
    monkeypatch.setenv("AUDIOBOOKS_DIR", str(root))
    client = MagicMock()
    client.get_audiobook_info.return_value = {"tracks": [_track(original, 17)]}
    link = tmp_path / "audio_cache" / "bookorbit_17" / "source_tracks"
    native_is_symlink = Path.is_symlink

    def is_symlink(candidate):
        return candidate == link or native_is_symlink(candidate)

    with patch.object(Path, "is_symlink", autospec=True, side_effect=is_symlink):
        with pytest.raises(ValueError, match="Unsafe BookOrbit audio cache"):
            BookOrbitAudioSourceAdapter(client, tmp_path).get_audio_files(
                "17", bridge_key="bookorbit:17")
    client.download_file_to_path.assert_not_called()
