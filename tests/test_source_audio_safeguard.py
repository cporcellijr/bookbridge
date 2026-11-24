"""Mounted-original safeguard and downloaded-vs-mounted parity.

Originals under the audiobook mount are read in place. These tests pin that a
job whose source changes or vanishes never publishes a map, that a retry
re-acquires the audio, and that the mounted route feeds the pipeline exactly
what the downloaded-copy route does. Every fixture lives in a temp dir.
"""

import hashlib
import logging
import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from src.db.database_service import DatabaseService
from src.db.models import Book
from src.services.alignment_service import AlignmentService
from src.services.audio_source_adapters import BookOrbitAudioSourceAdapter
from src.utils.forced_aligner import ForcedAligner
from src.utils.polisher import Polisher
from src.utils.source_fingerprint import AcquiredPaths, SourceAudioChanged, fingerprint, verify_unchanged
from src.utils.transcription_cancel import request_cancel
from src.utils.transcriber import TranscriptionCancelled
from tests.test_bookorbit_local_first import _copy_on_download, _track
from tests.test_storyteller_priority_flow import _build_manager

FFMPEG = shutil.which("ffmpeg")


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tone(path: Path, seconds: float = 6.0, freq: int = 220) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [FFMPEG, "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
         f"sine=frequency={freq}:duration={seconds}", "-ac", "1", "-ar", "16000", str(path)],
        check=True,
    )
    return path


def _mutate(kind: str, path: Path) -> None:
    if kind == "mtime":
        stat = path.stat()
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 7_000_000_000))
    elif kind == "size":
        path.write_bytes(path.read_bytes() + b"!")
    else:
        path.unlink()


# --------------------------------------------------------------------------- #
# The fingerprint helper
# --------------------------------------------------------------------------- #

def test_unchanged_source_passes(tmp_path):
    src = tmp_path / "a.mp3"
    src.write_bytes(b"audio")
    verify_unchanged("book", {str(src): fingerprint(str(src))}, "a seam")
    verify_unchanged("book", None, "a seam")


@pytest.mark.parametrize("kind", ["mtime", "size", "deleted"])
def test_changed_or_vanished_source_is_refused_and_logged(tmp_path, caplog, kind):
    src = tmp_path / "a.mp3"
    src.write_bytes(b"audio")
    fp = {str(src): fingerprint(str(src))}
    _mutate(kind, src)
    with caplog.at_level(logging.WARNING, logger="src.utils.source_fingerprint"):
        with pytest.raises(SourceAudioChanged):
            verify_unchanged("book-1", fp, "publishing the CTC map")
    assert "Source audio check:" in caplog.text
    assert "publishing the CTC map" in caplog.text


# --------------------------------------------------------------------------- #
# Acquisition: fingerprints, mixes, and failing closed
# --------------------------------------------------------------------------- #

def test_mounted_tracks_carry_a_fingerprint_and_downloaded_ones_do_not(tmp_path, monkeypatch):
    root = tmp_path / "audiobooks"
    root.mkdir()
    good = root / "one.mp3"
    good.write_bytes(b"first track bytes")
    outside = tmp_path / "elsewhere.mp3"
    outside.write_bytes(b"second track bytes")
    monkeypatch.setenv("AUDIOBOOKS_DIR", str(root))
    client = MagicMock()
    client.get_audiobook_info.return_value = {"tracks": [
        _track(good, 1, 5.0),
        _track(outside, 2, 7.0),  # outside the approved mount: must download
    ]}
    client.download_file_to_path.side_effect = _copy_on_download(outside)
    files = BookOrbitAudioSourceAdapter(client, tmp_path).get_audio_files("7", bridge_key="bookorbit:7")

    assert [f["track_index"] for f in files] == [0, 1]
    assert [f["duration_ms"] for f in files] == [5000, 7000]
    assert Path(files[0]["local_path"]) == good
    assert files[0]["source_fingerprint"] == (good.stat().st_size, good.stat().st_mtime_ns)
    assert "source_fingerprint" not in files[1]
    assert "audio_cache" in files[1]["local_path"]
    assert client.download_file_to_path.call_count == 1  # only the non-local track


def test_unusual_format_keeps_its_extension_in_the_download_route(tmp_path, monkeypatch):
    src = tmp_path / "x.wma"
    src.write_bytes(b"wma data")
    monkeypatch.setenv("AUDIOBOOKS_DIR", str(tmp_path / "nomount"))
    client = MagicMock()
    client.get_audiobook_info.return_value = {"tracks": [_track(src, 1, 1.0, format="wma")]}
    client.download_file_to_path.side_effect = _copy_on_download(src)
    files = BookOrbitAudioSourceAdapter(client, tmp_path).get_audio_files("1", bridge_key="k")
    assert files[0]["local_path"].endswith("track_000.wma")


@pytest.mark.parametrize("exc", [PermissionError("denied"), FileNotFoundError("gone")])
def test_stat_failure_after_validation_falls_back_to_download(tmp_path, monkeypatch, exc):
    root = tmp_path / "audiobooks"
    root.mkdir()
    src = root / "t.mp3"
    src.write_bytes(b"track bytes here")
    monkeypatch.setenv("AUDIOBOOKS_DIR", str(root))
    client = MagicMock()
    client.get_audiobook_info.return_value = {"tracks": [_track(src, 1)]}
    client.download_file_to_path.side_effect = _copy_on_download(src)
    with patch("src.services.audio_source_adapters.fingerprint", side_effect=exc):
        files = BookOrbitAudioSourceAdapter(client, tmp_path).get_audio_files("1", bridge_key="k")
    assert "audio_cache" in files[0]["local_path"]
    assert src.read_bytes() == b"track bytes here"


def test_permission_error_while_validating_falls_back(tmp_path, monkeypatch):
    root = tmp_path / "audiobooks"
    root.mkdir()
    src = root / "t.mp3"
    src.write_bytes(b"track bytes here")
    monkeypatch.setenv("AUDIOBOOKS_DIR", str(root))
    client = MagicMock()
    client.get_audiobook_info.return_value = {"tracks": [_track(src, 1)]}
    pristine = tmp_path / "pristine.mp3"
    pristine.write_bytes(src.read_bytes())
    client.download_file_to_path.side_effect = _copy_on_download(pristine)
    real_open = Path.open

    def deny(self, *args, **kwargs):
        if self == src.resolve():
            raise PermissionError("denied")
        return real_open(self, *args, **kwargs)

    with patch.object(Path, "open", deny):
        files = BookOrbitAudioSourceAdapter(client, tmp_path).get_audio_files("1", bridge_key="k")
    assert "audio_cache" in files[0]["local_path"]


@pytest.mark.parametrize("field", ["id", "size_bytes", "filename", "absolute_path", "duration_seconds"])
def test_missing_metadata_never_yields_a_half_trusted_local_track(tmp_path, monkeypatch, field):
    root = tmp_path / "audiobooks"
    root.mkdir()
    src = root / "t.mp3"
    src.write_bytes(b"track bytes here")
    monkeypatch.setenv("AUDIOBOOKS_DIR", str(root))
    track = _track(src, 1)
    track.pop(field)
    client = MagicMock()
    client.get_audiobook_info.return_value = {"tracks": [track]}
    client.download_file_to_path.side_effect = _copy_on_download(src)
    files = BookOrbitAudioSourceAdapter(client, tmp_path).get_audio_files("1", bridge_key="k")
    if field == "duration_seconds":
        assert files[0]["duration_ms"] is None  # same in both routes
        assert Path(files[0]["local_path"]) == src
    else:
        assert "audio_cache" in files[0]["local_path"]
    assert src.read_bytes() == b"track bytes here"


# --------------------------------------------------------------------------- #
# CTC seams: AlignmentService publishes only if the source still matches
# --------------------------------------------------------------------------- #

@pytest.fixture
def alignment(tmp_path, monkeypatch):
    monkeypatch.setenv("CTC_MODEL", "mms_fa")
    db = DatabaseService(str(tmp_path / "safeguard.db"))
    try:
        yield AlignmentService(db, Polisher())
    finally:
        db.db_manager.close()


def _fake_map():
    return [{"char": c, "ts": c / 10.0} for c in range(0, 101, 10)]


def test_map_is_not_published_when_the_source_changes_during_the_model_pass(alignment):
    calls = []

    def check(seam):
        calls.append(seam)
        if seam == "publishing the CTC map":
            raise SourceAudioChanged("changed")

    with patch.object(ForcedAligner, "is_available", return_value=True), \
         patch.object(ForcedAligner, "can_single_pass", return_value=True), \
         patch.object(ForcedAligner, "align", return_value=_fake_map()), \
         patch.object(AlignmentService, "_publish_map") as publish:
        with pytest.raises(SourceAudioChanged):
            alignment.align_forced_and_store("b", ["/a.m4b"], "x" * 100, source_check=check)
    assert calls == ["the CTC model pass", "publishing the CTC map"]
    publish.assert_not_called()


def test_map_is_published_when_the_source_is_unchanged(alignment):
    with patch.object(ForcedAligner, "is_available", return_value=True), \
         patch.object(ForcedAligner, "can_single_pass", return_value=True), \
         patch.object(ForcedAligner, "align", return_value=_fake_map()):
        assert alignment.align_forced_and_store(
            "b", ["/a.m4b"], "x" * 100, source_check=lambda seam: None) is True


def test_try_ctc_alignment_reraises_source_change_and_passes_a_check_only_for_mounted_paths(tmp_path):
    manager, _db, _abs, _tr, alignment_service = _build_manager(tmp_path)
    alignment_service.align_forced_and_store.return_value = True
    manager._try_ctc_alignment("b", ["/a.m4b"], "t", None, "T", 1.0, source_label="attempt")
    assert "source_check" not in alignment_service.align_forced_and_store.call_args.kwargs

    manager._try_ctc_alignment(
        "b", AcquiredPaths(["/a.m4b"], {"/a.m4b": (1, 2)}), "t", None, "T", 1.0, source_label="attempt")
    assert callable(alignment_service.align_forced_and_store.call_args.kwargs["source_check"])

    alignment_service.align_forced_and_store.side_effect = SourceAudioChanged("x")
    with pytest.raises(SourceAudioChanged):
        manager._try_ctc_alignment("b", ["/a.m4b"], "t", None, "T", 1.0, source_label="attempt")


# --------------------------------------------------------------------------- #
# Whole job: change/vanish -> failed_retry_later, nothing published, retry re-acquires
# --------------------------------------------------------------------------- #

def _job_setup(tmp_path, monkeypatch, tracks=2):
    root = tmp_path / "audiobooks"
    root.mkdir()
    sources = []
    for i in range(tracks):
        p = root / f"{i}.mp3"
        p.write_bytes(f"original track {i} bytes".encode() * 10)
        sources.append(p)
    originals = {p: p.read_bytes() for p in sources}
    monkeypatch.setenv("AUDIOBOOKS_DIR", str(root))
    monkeypatch.setenv("CTC_ENABLED", "true")
    manager, db, _abs, transcriber, alignment_service = _build_manager(tmp_path)
    transcriber.transcribe_from_smil.return_value = None
    client = MagicMock()
    client.get_audiobook_info.return_value = {"tracks": [_track(p, 10 + i) for i, p in enumerate(sources)]}
    adapter = BookOrbitAudioSourceAdapter(client, tmp_path)
    adapter.get_chapters = MagicMock(return_value=[{"start": 0.0, "end": 12.0}])
    manager.audio_source_adapters = {"BookOrbit": adapter}
    book = Book(abs_id="bo-1", abs_title="Mounted Book", ebook_filename="book.epub",
                kosync_doc_id="h", status="pending", duration=12.0,
                audio_source="BookOrbit", audio_source_id="77", sync_mode="audiobook")
    return manager, db, client, transcriber, alignment_service, book, sources, originals


@pytest.mark.parametrize("kind", ["mtime", "size", "deleted"])
def test_ctc_job_fails_for_retry_when_a_source_changes_before_the_map_is_published(
        tmp_path, monkeypatch, kind, caplog):
    manager, db, client, transcriber, alignment_service, book, sources, _o = _job_setup(tmp_path, monkeypatch)

    def align(abs_id, paths, text, **kwargs):
        _mutate(kind, sources[1])
        kwargs["source_check"]("publishing the CTC map")
        return True

    alignment_service.align_forced_and_store.side_effect = align
    with caplog.at_level(logging.WARNING):
        manager._run_background_job(book)

    assert book.status == "failed_retry_later"
    assert book.transcript_source != "ctc"
    transcriber.process_audio.assert_not_called()      # no Whisper fallback on changed audio
    alignment_service.align_and_store.assert_not_called()
    assert "Source audio check:" in caplog.text
    client.download_file_to_path.assert_not_called()
    assert sources[0].read_bytes().startswith(b"original track 0")


def test_retry_after_a_changed_source_reacquires_and_downloads_when_local_validation_fails(
        tmp_path, monkeypatch):
    manager, db, client, transcriber, alignment_service, book, sources, originals = _job_setup(tmp_path, monkeypatch)
    seen_paths = []
    pristine = tmp_path / "pristine.mp3"
    pristine.write_bytes(originals[sources[1]])

    def first(abs_id, paths, text, **kwargs):
        sources[1].write_bytes(originals[sources[1]] + b"!")   # re-released: size now differs from the API
        kwargs["source_check"]("publishing the CTC map")

    alignment_service.align_forced_and_store.side_effect = first
    manager._run_background_job(book)
    assert book.status == "failed_retry_later"

    def second(abs_id, paths, text, **kwargs):
        seen_paths.extend(paths)
        return True

    alignment_service.align_forced_and_store.side_effect = second
    client.download_file_to_path.side_effect = _copy_on_download(pristine)
    manager._run_background_job(book)
    assert Path(seen_paths[0]) == sources[0]
    assert "audio_cache" in seen_paths[1]
    assert book.transcript_source == "ctc"


def test_whisper_job_fails_for_retry_when_a_source_changes_before_alignment_is_stored(
        tmp_path, monkeypatch):
    manager, db, client, transcriber, alignment_service, book, sources, _o = _job_setup(tmp_path, monkeypatch)
    monkeypatch.setenv("CTC_ENABLED", "false")

    def process(abs_id, files, **kwargs):
        assert all("source_fingerprint" in f for f in files)
        _mutate("mtime", sources[0])
        return [{"start": 0.0, "end": 1.0, "text": "x"}]

    transcriber.process_audio.side_effect = process
    manager._run_background_job(book)
    assert book.status == "failed_retry_later"
    alignment_service.align_and_store.assert_not_called()


def test_cancelled_worker_leaves_originals_alone_and_reports_nothing(tmp_path, monkeypatch):
    manager, db, client, transcriber, alignment_service, book, sources, originals = _job_setup(tmp_path, monkeypatch)

    def align(abs_id, paths, text, **kwargs):
        request_cancel(abs_id)
        _mutate("mtime", sources[0])      # even a change seen by a cancelled worker is not reported
        raise TranscriptionCancelled(abs_id)

    alignment_service.align_forced_and_store.side_effect = align
    manager._run_background_job(book)
    assert book.status == "pending"        # no failure written by the cancelled generation
    db.save_job.assert_not_called()
    assert sources[1].read_bytes() == originals[sources[1]]
    assert sources[0].exists()


def test_two_jobs_sharing_the_same_sources_keep_independent_baselines(tmp_path, monkeypatch):
    manager, db, client, transcriber, alignment_service, book, sources, originals = _job_setup(tmp_path, monkeypatch)
    adapter = manager.audio_source_adapters["BookOrbit"]
    first = adapter.get_audio_files("77", bridge_key="a")
    second = adapter.get_audio_files("77", bridge_key="b")
    _mutate("mtime", sources[0])
    with pytest.raises(SourceAudioChanged):
        verify_unchanged("a", {f["local_path"]: tuple(f["source_fingerprint"]) for f in first}, "x")
    third = adapter.get_audio_files("77", bridge_key="b")
    verify_unchanged("b", {f["local_path"]: tuple(f["source_fingerprint"]) for f in third}, "x")
    assert second[0]["source_fingerprint"] != third[0]["source_fingerprint"]
    assert not (tmp_path / "audio_cache").exists()
    assert all(p.read_bytes() == originals[p] for p in sources)


# --------------------------------------------------------------------------- #
# Parity: downloaded-copy route vs mounted route, with real decode
# --------------------------------------------------------------------------- #

@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not installed")
def test_multitrack_decode_is_identical_between_routes_with_offsets(tmp_path, monkeypatch):
    from src.utils.quartznet_aligner import QuartzNetAligner
    root = tmp_path / "audiobooks"
    tracks = [_tone(root / "a.wav", 3.0, 220), _tone(root / "b.wav", 5.0, 330), _tone(root / "c.wav", 2.0, 440)]
    before = [_digest(p) for p in tracks]
    monkeypatch.setenv("AUDIOBOOKS_DIR", str(root))
    client = MagicMock()
    client.get_audiobook_info.return_value = {"tracks": [
        _track(p, 90 + i, d) for i, (p, d) in enumerate(zip(tracks, (3.0, 5.0, 2.0)))]}

    mounted = BookOrbitAudioSourceAdapter(client, tmp_path / "m").get_audio_files("5", bridge_key="x")
    monkeypatch.setenv("AUDIOBOOKS_DIR", str(tmp_path / "nomount"))

    def download(file_id, dest):
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(tracks[file_id - 90], dest)
        return True

    client.download_file_to_path.side_effect = download
    downloaded = BookOrbitAudioSourceAdapter(client, tmp_path / "d").get_audio_files("5", bridge_key="x")

    assert [f["track_index"] for f in mounted] == [f["track_index"] for f in downloaded] == [0, 1, 2]
    assert [f["duration_ms"] for f in mounted] == [f["duration_ms"] for f in downloaded]
    assert [f["ext"] for f in mounted] == [f["ext"] for f in downloaded]
    pcm_m = np.concatenate(list(QuartzNetAligner._pcm_windows([f["local_path"] for f in mounted])))
    pcm_d = np.concatenate(list(QuartzNetAligner._pcm_windows([f["local_path"] for f in downloaded])))
    assert pcm_m.shape == pcm_d.shape and np.array_equal(pcm_m, pcm_d)
    assert abs(pcm_m.shape[0] / 16000 - 10.0) < 0.1       # track offsets accumulate: 3 + 5 + 2 s
    assert [_digest(p) for p in tracks] == before


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not installed")
def test_readalong_audio_transcode_is_identical_between_routes(tmp_path):
    from src.services.readalong_builder import _transcode_audio_for_embed
    src = _tone(tmp_path / "audiobooks" / "t.wav", 4.0)
    cached = tmp_path / "cache" / "source_tracks" / "track_000.wav"
    cached.parent.mkdir(parents=True)
    shutil.copyfile(src, cached)
    before = _digest(src)
    out_m, out_c = tmp_path / "m.m4a", tmp_path / "c.m4a"
    assert _transcode_audio_for_embed([src], "64k", out_m)
    assert _transcode_audio_for_embed([cached], "64k", out_c)
    assert _digest(out_m) == _digest(out_c)
    assert _digest(src) == before


def _quartznet_model():
    from src.utils import quartznet_aligner as qn
    configured = os.environ.get("CTC_QUARTZNET_MODEL_PATH", "").strip()
    candidates = [Path(configured)] if configured else []
    candidates.append(Path(os.environ.get("DATA_DIR", "/data")) / "models" / "quartznet15x5-en")
    for candidate in candidates:
        model = candidate / "model.onnx" if candidate.is_dir() else candidate
        if model.is_file() and model.stat().st_size == qn._MODEL_BYTES:
            return model
    return None


@pytest.mark.skipif(not FFMPEG or _quartznet_model() is None,
                    reason="ffmpeg or a verified local QuartzNet model is unavailable")
def test_real_quartznet_emissions_and_map_are_identical_between_routes(tmp_path, monkeypatch):
    from src.utils.quartznet_aligner import QuartzNetAligner
    monkeypatch.setenv("CTC_QUARTZNET_MODEL_PATH", str(_quartznet_model()))
    original = _tone(tmp_path / "audiobooks" / "t.wav", 12.0, 300)
    cached = tmp_path / "audio_cache" / "k" / "source_tracks" / "track_000.wav"
    cached.parent.mkdir(parents=True)
    shutil.copyfile(original, cached)
    before = _digest(original)
    text = "the quick brown fox jumps over the lazy dog and runs away"

    results = []
    for path in (cached, original):
        aligner = QuartzNetAligner()
        emission, spf = aligner.emissions_for([str(path)])
        results.append((np.asarray(emission), spf, aligner.align([str(path)], text)))
    assert np.array_equal(results[0][0], results[1][0])
    assert results[0][1] == results[1][1]
    assert results[0][2] == results[1][2]
    assert _digest(original) == before


# --------------------------------------------------------------------------- #
# Review round 2
# --------------------------------------------------------------------------- #

def _real_whisper_job(tmp_path, monkeypatch):
    """A job over two real 6 s tone tracks on a mount, with the real transcriber."""
    from src.utils import transcriber as transcriber_module
    from src.utils.transcriber import AudioTranscriber

    manager, db, client, _tr, alignment_service, book, sources, originals = _job_setup(tmp_path, monkeypatch)
    monkeypatch.setenv("CTC_ENABLED", "false")
    for i, p in enumerate(sources):
        _tone(p.with_suffix(".wav"), 6.0, 200 + 50 * i)
        p.unlink()
    wavs = [p.with_suffix(".wav") for p in sources]
    client.get_audiobook_info.return_value = {"tracks": [
        _track(p, 10 + i, 6.0, format="wav") for i, p in enumerate(wavs)]}
    manager.transcriber = AudioTranscriber(tmp_path, MagicMock(), MagicMock())
    manager.transcriber.transcribe_from_smil = MagicMock(return_value=None)
    provider = MagicMock()
    provider.supports_raw_audio = False
    provider.last_vad_filter = False
    provider.get_name.return_value = "sim"
    monkeypatch.setattr(transcriber_module, "get_transcription_provider", lambda: provider)
    return manager, alignment_service, book, wavs, provider


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not installed")
def test_rejected_whisper_transcript_is_not_reused_by_the_retry(tmp_path, monkeypatch):
    manager, alignment_service, book, wavs, provider = _real_whisper_job(tmp_path, monkeypatch)
    calls = {"n": 0}

    def transcribe(_path):
        calls["n"] += 1
        label = f"old{calls['n']}" if calls["n"] <= 2 else f"new{calls['n']}"
        if calls["n"] == 1:   # same-size content rewrite, changed mtime, during attempt 1
            wavs[1].write_bytes(_tone(tmp_path / "other.wav", 6.0, 777).read_bytes())
            stat = wavs[1].stat()
            os.utime(wavs[1], ns=(stat.st_atime_ns, stat.st_mtime_ns + 9_000_000_000))
        return [{"start": 0.0, "end": 6.0, "text": label}]

    provider.transcribe.side_effect = transcribe
    manager._run_background_job(book)
    assert book.status == "failed_retry_later"
    alignment_service.align_and_store.assert_not_called()
    first_attempt_calls = calls["n"]

    alignment_service.align_and_store.return_value = True
    manager._run_background_job(book)

    assert calls["n"] > first_attempt_calls            # the provider ran again
    transcript = alignment_service.align_and_store.call_args.args[1]
    assert all(seg["text"].startswith("new") for seg in transcript)


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not installed")
def test_progress_without_recorded_fingerprints_still_resumes(tmp_path, monkeypatch):
    """Progress written for downloaded-copy books has no fingerprints and keeps resuming."""
    manager, alignment_service, book, wavs, provider = _real_whisper_job(tmp_path, monkeypatch)
    import json
    progress_dir = tmp_path / "audio_cache" / "bo-1"
    progress_dir.mkdir(parents=True)
    (progress_dir / "_progress.json").write_text(json.dumps({
        "chunks_completed": 2, "cumulative_duration": 12.0, "vad_filter": False, "done": True,
        "transcript": [{"start": 0.0, "end": 12.0, "text": "legacy"}]}))
    alignment_service.align_and_store.return_value = True
    manager._run_background_job(book)
    provider.transcribe.assert_not_called()
    assert alignment_service.align_and_store.call_args.args[1][0]["text"] == "legacy"


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not installed")
def test_partial_progress_from_other_audio_is_discarded(tmp_path, monkeypatch):
    manager, alignment_service, book, wavs, provider = _real_whisper_job(tmp_path, monkeypatch)
    import json
    from src.utils.source_fingerprint import fingerprint
    progress_dir = tmp_path / "audio_cache" / "bo-1"
    progress_dir.mkdir(parents=True)
    stale = {str(wavs[0]): [1, 2], str(wavs[1]): [1, 2]}
    (progress_dir / "_progress.json").write_text(json.dumps({
        "chunks_completed": 1, "cumulative_duration": 6.0, "vad_filter": False, "done": False,
        "source_fingerprints": stale, "transcript": [{"start": 0.0, "end": 6.0, "text": "stale"}]}))
    provider.transcribe.side_effect = lambda _p: [{"start": 0.0, "end": 6.0, "text": "fresh"}]
    alignment_service.align_and_store.return_value = True
    manager._run_background_job(book)
    assert provider.transcribe.call_count == 2
    assert all(s["text"] == "fresh" for s in alignment_service.align_and_store.call_args.args[1])


@pytest.mark.parametrize("site", ["attempt", "upgrade"])
def test_source_vanishing_during_decode_fails_for_retry_instead_of_falling_back(
        tmp_path, monkeypatch, site, caplog):
    manager, db, client, transcriber, alignment_service, book, sources, _o = _job_setup(tmp_path, monkeypatch)
    if site == "upgrade":
        # first pass is refused (no map yet), then transcription runs, then the upgrade pass decodes
        transcriber.process_audio.return_value = [{"start": 0.0, "end": 1.0, "text": "x"}]
        alignment_service.align_and_store.return_value = True
        alignment_service.align_forced_and_store.side_effect = [False, None]
    calls = {"n": 0}

    def decode_fails(*_a, **_k):
        calls["n"] += 1
        if site == "attempt" or calls["n"] == 2:
            sources[1].unlink()
            raise RuntimeError("ffmpeg could not decode")
        return False

    alignment_service.align_forced_and_store.side_effect = decode_fails
    with caplog.at_level(logging.WARNING):
        manager._run_background_job(book)
    assert book.status == "failed_retry_later"
    assert "Source audio check:" in caplog.text
    if site == "attempt":
        transcriber.process_audio.assert_not_called()


def test_decoder_failure_with_unchanged_sources_still_falls_back(tmp_path, monkeypatch):
    manager, db, client, transcriber, alignment_service, book, sources, _o = _job_setup(tmp_path, monkeypatch)
    alignment_service.align_forced_and_store.side_effect = RuntimeError("model blew up")
    alignment_service.align_and_store.return_value = True
    transcriber.process_audio.return_value = [{"start": 0.0, "end": 1.0, "text": "x"}]
    manager._run_background_job(book)
    transcriber.process_audio.assert_called_once()
    assert book.status != "failed_retry_later"


# --------------------------------------------------------------------------- #
# Reparse points and the read-along pre-packaging check
# --------------------------------------------------------------------------- #

def _junction(link: Path, target: Path) -> bool:
    if os.name != "nt":
        return False
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                            capture_output=True)
    return result.returncode == 0 and link.exists()


def test_junction_inside_the_mount_is_not_trusted(tmp_path, monkeypatch):
    root = tmp_path / "audiobooks"
    real_dir = root / "real"
    other_dir = root / "other"
    real_dir.mkdir(parents=True)
    other_dir.mkdir()
    (real_dir / "t.mp3").write_bytes(b"recording A....")
    (other_dir / "t.mp3").write_bytes(b"recording B....")       # same basename and size
    if not _junction(root / "link", other_dir):
        pytest.skip("directory junctions cannot be created here")
    monkeypatch.setenv("AUDIOBOOKS_DIR", str(root))
    claimed = root / "link" / "t.mp3"
    track = _track(real_dir / "t.mp3", 1, absolute_path=str(claimed))
    client = MagicMock()
    client.get_audiobook_info.return_value = {"tracks": [track]}
    client.download_file_to_path.side_effect = _copy_on_download(real_dir / "t.mp3")
    files = BookOrbitAudioSourceAdapter(client, tmp_path).get_audio_files("1", bridge_key="k")
    assert "audio_cache" in files[0]["local_path"]
    assert "source_fingerprint" not in files[0]


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not installed")
def test_readalong_build_refuses_when_a_source_changes_before_packaging(tmp_path):
    from src.services import readalong_builder as rb
    from tests.test_readalong_builder import _build, _make_audio, _parser, _write_epub

    parser = _parser(tmp_path)
    epub_path = tmp_path / "books" / "book.epub"
    _write_epub(epub_path, {"ch1": b"<html><body><p>Alpha bravo.</p></body></html>"})
    combined_text, _ = parser.extract_text_and_map(str(epub_path))
    audio = _make_audio(tmp_path, suffix=".wav")
    real_transcode = rb._transcode_audio_for_embed

    def transcode_then_source_changes(*args, **kwargs):
        ok = real_transcode(*args, **kwargs)
        _mutate("mtime", audio)       # the original changes after the build started
        return ok

    with patch.object(rb, "_transcode_audio_for_embed", transcode_then_source_changes):
        result, output_path = _build(tmp_path, parser, epub_path, audio, combined_text)
    assert result is None
    assert not output_path.exists()


def test_discarding_stale_progress_keeps_tracks_downloaded_for_this_run(tmp_path):
    """A mixed book (some tracks mounted, some just downloaded) must keep its downloads."""
    from src.utils.transcriber import AudioTranscriber

    cache = tmp_path / "audio_cache" / "bookorbit_7"
    downloaded = cache / "source_tracks" / "track_001.mp3"
    downloaded.parent.mkdir(parents=True)
    downloaded.write_bytes(b"fresh download")
    stale_part = cache / "part_000.wav"
    stale_part.write_bytes(b"old decode")
    progress = cache / "_progress.json"
    progress.write_text('{"source_fingerprints": {"/audiobooks/a.mp3": [1, 1]}, "done": true}')

    transcriber = AudioTranscriber.__new__(AudioTranscriber)
    transcriber.cache_root = tmp_path / "audio_cache"
    AudioTranscriber._discard_progress_from_other_audio(
        transcriber, "bookorbit:7", cache, progress,
        {"/audiobooks/a.mp3": [1, 2]},
    )

    assert not progress.exists()
    assert not stale_part.exists()
    assert downloaded.read_bytes() == b"fresh download"
