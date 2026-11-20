import json
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.utils.storyteller_transcript import StorytellerTranscript
from src.utils.transcriber import AudioTranscriber
from src.services.alignment_service import (
    _storyteller_filename_for_abs_chapter,
    _validate_storyteller_chapters,
    ingest_storyteller_transcripts,
)


def _chapter_payload(transcript_text, words):
    return {
        "transcript": transcript_text,
        "wordTimeline": [
            {
                "type": "word",
                "text": word["text"],
                "startTime": word["start"],
                "endTime": word["end"],
                "startOffsetUtf16": word["start_offset"],
                "endOffsetUtf16": word["end_offset"],
                "timeline": [],
            }
            for word in words
        ],
    }


def _write_wordtimeline_file(transcriptions_dir: Path, filename: str, text: str = "hello world"):
    (transcriptions_dir / filename).write_text(
        json.dumps({"transcript": text, "wordTimeline": []}),
        encoding="utf-8",
    )


def _write_storyteller_fixture(base_dir: Path):
    transcript_dir = base_dir / "storyteller"
    transcript_dir.mkdir(parents=True, exist_ok=True)

    chapter1 = _chapter_payload(
        "hello world",
        [
            {"text": "hello", "start": 0.5, "end": 0.9, "start_offset": 0, "end_offset": 5},
            {"text": "world", "start": 1.0, "end": 1.4, "start_offset": 6, "end_offset": 11},
        ],
    )
    chapter2 = _chapter_payload(
        "second chapter",
        [
            {"text": "second", "start": 0.2, "end": 0.6, "start_offset": 0, "end_offset": 6},
            {"text": "chapter", "start": 1.2, "end": 1.6, "start_offset": 7, "end_offset": 14},
        ],
    )

    chapter1_name = "00000-00001.json"
    chapter2_name = "00000-00002.json"
    (transcript_dir / chapter1_name).write_text(json.dumps(chapter1), encoding="utf-8")
    (transcript_dir / chapter2_name).write_text(json.dumps(chapter2), encoding="utf-8")

    manifest = {
        "format": "storyteller_manifest",
        "version": 1,
        "duration": 20.0,
        "chapters": [
            {"index": 0, "file": chapter1_name, "start": 0.0, "end": 10.0},
            {"index": 1, "file": chapter2_name, "start": 10.0, "end": 20.0},
        ],
    }
    manifest_path = transcript_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path, chapter1, chapter2


def test_storyteller_filename_mapping():
    assert _storyteller_filename_for_abs_chapter(0) == "00000-00001.json"
    assert _storyteller_filename_for_abs_chapter(1) == "00000-00002.json"
    assert _storyteller_filename_for_abs_chapter(9) == "00000-00010.json"


def test_storyteller_validation_accepts_00000_prefix(tmp_path):
    transcriptions_dir = tmp_path / "transcriptions_00000"
    transcriptions_dir.mkdir(parents=True, exist_ok=True)
    _write_wordtimeline_file(transcriptions_dir, "00000-00001.json")
    _write_wordtimeline_file(transcriptions_dir, "00000-00002.json")

    is_valid, source_files, destination_files = _validate_storyteller_chapters(transcriptions_dir)
    assert is_valid is True
    assert source_files == ["00000-00001.json", "00000-00002.json"]
    assert destination_files == ["00000-00001.json", "00000-00002.json"]


def test_storyteller_validation_accepts_00001_prefix(tmp_path):
    transcriptions_dir = tmp_path / "transcriptions"
    transcriptions_dir.mkdir(parents=True, exist_ok=True)

    for idx in range(2):
        _write_wordtimeline_file(transcriptions_dir, f"00001-{idx + 1:05d}.json")

    is_valid, source_files, destination_files = _validate_storyteller_chapters(transcriptions_dir)
    assert is_valid is True
    assert source_files == ["00001-00001.json", "00001-00002.json"]
    assert destination_files == ["00000-00001.json", "00000-00002.json"]


def test_storyteller_validation_accepts_chapter_first_zero_based(tmp_path):
    transcriptions_dir = tmp_path / "transcriptions_chapter_first_zero_based"
    transcriptions_dir.mkdir(parents=True, exist_ok=True)
    _write_wordtimeline_file(transcriptions_dir, "00000-00001.json")
    _write_wordtimeline_file(transcriptions_dir, "00001-00001.json")

    is_valid, source_files, destination_files = _validate_storyteller_chapters(transcriptions_dir)
    assert is_valid is True
    assert source_files == ["00000-00001.json", "00001-00001.json"]
    assert destination_files == ["00000-00001.json", "00000-00002.json"]


def test_storyteller_validation_accepts_chapter_first_one_based(tmp_path):
    transcriptions_dir = tmp_path / "transcriptions_chapter_first_one_based"
    transcriptions_dir.mkdir(parents=True, exist_ok=True)
    _write_wordtimeline_file(transcriptions_dir, "00001-00001.json")
    _write_wordtimeline_file(transcriptions_dir, "00002-00001.json")

    is_valid, source_files, destination_files = _validate_storyteller_chapters(transcriptions_dir)
    assert is_valid is True
    assert source_files == ["00001-00001.json", "00002-00001.json"]
    assert destination_files == ["00000-00001.json", "00000-00002.json"]


def test_storyteller_validation_accepts_any_count(tmp_path):
    # 3 valid files — validation has no count expectation, works with whatever is present.
    transcriptions_dir = tmp_path / "transcriptions_count_mismatch"
    transcriptions_dir.mkdir(parents=True, exist_ok=True)
    _write_wordtimeline_file(transcriptions_dir, "00001-00001.json")
    _write_wordtimeline_file(transcriptions_dir, "00002-00001.json")
    _write_wordtimeline_file(transcriptions_dir, "00003-00001.json")

    is_valid, source_files, destination_files = _validate_storyteller_chapters(transcriptions_dir)
    assert is_valid is True
    assert source_files == ["00001-00001.json", "00002-00001.json", "00003-00001.json"]
    assert destination_files == ["00000-00001.json", "00000-00002.json", "00000-00003.json"]


def test_storyteller_validation_rejects_non_wordtimeline_format(tmp_path):
    transcriptions_dir = tmp_path / "transcriptions_invalid"
    transcriptions_dir.mkdir(parents=True, exist_ok=True)

    segment_like = [{"start": 0.0, "end": 1.0, "text": "hello"}]
    (transcriptions_dir / "00001-00001.json").write_text(json.dumps(segment_like), encoding="utf-8")

    is_valid, source_files, destination_files = _validate_storyteller_chapters(transcriptions_dir)
    assert is_valid is False
    assert source_files == []
    assert destination_files == []


def test_storyteller_ingest_rewrites_chapter_first_layout_to_canonical(tmp_path, monkeypatch):
    assets_root = tmp_path / "storyteller_assets"
    data_dir = tmp_path / "data"
    transcriptions_dir = assets_root / "assets" / "Book One" / "transcriptions"
    transcriptions_dir.mkdir(parents=True, exist_ok=True)
    _write_wordtimeline_file(transcriptions_dir, "00001-00001.json", text="chapter one")
    _write_wordtimeline_file(transcriptions_dir, "00002-00001.json", text="chapter two")

    monkeypatch.setenv("STORYTELLER_ASSETS_DIR", str(assets_root))
    monkeypatch.setenv("DATA_DIR", str(data_dir))

    manifest_path = ingest_storyteller_transcripts(
        "abs-ingest-1",
        "Book One",
        [{"start": 0.0, "end": 10.0}, {"start": 10.0, "end": 20.0}],
    )

    assert manifest_path is not None
    manifest_file = Path(manifest_path)
    assert manifest_file.exists()
    target_dir = data_dir / "transcripts" / "storyteller" / "abs-ingest-1"
    manifest_payload = json.loads(manifest_file.read_text(encoding="utf-8"))
    chapter_files = [chapter["file"] for chapter in manifest_payload.get("chapters", [])]
    assert [Path(name).name for name in chapter_files] == ["00000-00001.json", "00000-00002.json"]
    assert all((target_dir / name).exists() for name in chapter_files)
    assert not (target_dir / "00001-00001.json").exists()
    assert not (target_dir / "00002-00001.json").exists()


def test_storyteller_ingest_republishes_without_exposing_stale_chapters(tmp_path, monkeypatch):
    assets_root = tmp_path / "storyteller_assets"
    data_dir = tmp_path / "data"
    transcriptions_dir = assets_root / "assets" / "Book Two" / "transcriptions"
    transcriptions_dir.mkdir(parents=True, exist_ok=True)
    _write_wordtimeline_file(transcriptions_dir, "00001-00001.json", text="chapter one")
    _write_wordtimeline_file(transcriptions_dir, "00002-00001.json", text="chapter two")
    _write_wordtimeline_file(transcriptions_dir, "00003-00001.json", text="chapter three")

    monkeypatch.setenv("STORYTELLER_ASSETS_DIR", str(assets_root))
    monkeypatch.setenv("DATA_DIR", str(data_dir))

    first_manifest = ingest_storyteller_transcripts(
        "abs-ingest-2",
        "Book Two",
        [{"start": 0.0, "end": 10.0}, {"start": 10.0, "end": 20.0}, {"start": 20.0, "end": 30.0}],
    )
    assert first_manifest is not None
    target_dir = data_dir / "transcripts" / "storyteller" / "abs-ingest-2"
    previous_chapter_files = [
        target_dir / chapter["file"]
        for chapter in json.loads(Path(first_manifest).read_text())["chapters"]
    ]
    assert len(previous_chapter_files) == 3
    assert all(file.exists() for file in previous_chapter_files)

    (transcriptions_dir / "00003-00001.json").unlink()
    second_manifest = ingest_storyteller_transcripts(
        "abs-ingest-2",
        "Book Two",
        [{"start": 0.0, "end": 10.0}, {"start": 10.0, "end": 20.0}],
    )
    assert second_manifest is not None

    target_dir = data_dir / "transcripts" / "storyteller" / "abs-ingest-2"
    published_files = [ch["file"] for ch in json.loads(Path(second_manifest).read_text())["chapters"]]
    assert [Path(name).name for name in published_files] == ["00000-00001.json", "00000-00002.json"]
    assert all((target_dir / name).exists() for name in published_files)
    assert not (target_dir / "00000-00003.json").exists()
    # Readers holding the old manifest still have its immutable transcript set.
    assert all(file.exists() for file in previous_chapter_files)
    assert all(str(file.relative_to(target_dir)) not in published_files
               for file in previous_chapter_files)


def test_storyteller_ingest_parallel_publish_never_removes_inflight_copy(tmp_path, monkeypatch):
    """9904: concurrent backfills cannot unlink a file before copy2/copystat finishes."""
    assets_root = tmp_path / "assets_root"
    source_dir = assets_root / "assets" / "Race Book" / "transcriptions"
    source_dir.mkdir(parents=True)
    for i in (1, 2):
        _write_wordtimeline_file(source_dir, f"00001-{i:05d}.json", f"chapter {i}")
    monkeypatch.setenv("STORYTELLER_ASSETS_DIR", str(assets_root))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    chapters = [{"start": 0.0, "end": 2.0}, {"start": 2.0, "end": 4.0}]
    first_copy = threading.Event()
    second_copy = threading.Event()
    first_done = threading.Event()
    real_copy2 = shutil.copy2
    blocked = False

    def controlled_copy2(src, dst, *args, **kwargs):
        nonlocal blocked
        if threading.current_thread().name.startswith("first") and not blocked:
            blocked = True
            result = real_copy2(src, dst, *args, **kwargs)
            first_copy.set()
            assert second_copy.wait(10), "second ingest did not reach its copy"
            if not Path(dst).exists():
                raise FileNotFoundError(2, "No such file after copy2", str(dst))
            return result
        if threading.current_thread().name.startswith("second"):
            second_copy.set()
            assert first_done.wait(10), "first ingest did not finish"
        return real_copy2(src, dst, *args, **kwargs)

    def first():
        try:
            return ingest_storyteller_transcripts("abs-race", "Race Book", chapters)
        finally:
            first_done.set()

    def second():
        return ingest_storyteller_transcripts("abs-race", "Race Book", chapters)

    with patch("src.services.alignment_service.shutil.copy2", side_effect=controlled_copy2):
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="first") as executor:
            first_future = executor.submit(first)
            assert first_copy.wait(10)
            # Give the second worker a distinct name to control the interleaving.
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="second") as other:
                second_future = other.submit(second)
                first_manifest = first_future.result(timeout=15)
                second_manifest = second_future.result(timeout=15)

    assert first_manifest == second_manifest
    reader = StorytellerTranscript(first_manifest)
    assert reader.chapter_count() == 2


def test_storyteller_ingest_failed_republish_preserves_last_valid_manifest(tmp_path, monkeypatch):
    """9904: a failed copy does not corrupt an earlier published transcript set."""
    assets_root = tmp_path / "assets_root"
    source_dir = assets_root / "assets" / "Rescue Book" / "transcriptions"
    source_dir.mkdir(parents=True)
    for i in (1, 2):
        _write_wordtimeline_file(source_dir, f"00001-{i:05d}.json", f"old {i}")
    monkeypatch.setenv("STORYTELLER_ASSETS_DIR", str(assets_root))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    two = [{"start": 0, "end": 1}, {"start": 1, "end": 2}]
    manifest_path = Path(ingest_storyteller_transcripts("abs-rescue", "Rescue Book", two))
    old_manifest = manifest_path.read_bytes()
    old_chapters = [
        (manifest_path.parent / ch["file"]).read_bytes()
        for ch in json.loads(old_manifest)["chapters"]
    ]

    for i in (1, 2, 3):
        _write_wordtimeline_file(source_dir, f"00001-{i:05d}.json", f"NEW {i}")
    real_copy = shutil.copy2
    count = 0

    def fail_last_copy(src, dst, *args, **kwargs):
        nonlocal count
        count += 1
        if count == 3:
            raise OSError(112, "Host is down", str(src))
        return real_copy(src, dst, *args, **kwargs)

    with patch("src.services.alignment_service.shutil.copy2", side_effect=fail_last_copy):
        import pytest
        with pytest.raises(OSError, match="Host is down"):
            ingest_storyteller_transcripts("abs-rescue", "Rescue Book", two)

    assert manifest_path.read_bytes() == old_manifest
    assert [
        (manifest_path.parent / ch["file"]).read_bytes()
        for ch in json.loads(old_manifest)["chapters"]
    ] == old_chapters
    assert StorytellerTranscript(manifest_path).chapter_count() == 2


def test_storyteller_ingest_chapterless_mode_builds_manifest_from_transcripts(tmp_path, monkeypatch):
    assets_root = tmp_path / "storyteller_assets"
    data_dir = tmp_path / "data"
    transcriptions_dir = assets_root / "assets" / "Book Three" / "transcriptions"
    transcriptions_dir.mkdir(parents=True, exist_ok=True)

    chapter_one = {
        "transcript": "chapter one text",
        "wordTimeline": [
            {"startTime": 0.0, "endTime": 4.0},
            {"startTime": 4.0, "endTime": 5.0},
        ],
    }
    chapter_two = {
        "transcript": "chapter two text",
        "wordTimeline": [
            {"startTime": 0.0, "endTime": 2.0},
            {"startTime": 2.0, "endTime": 3.0},
        ],
    }
    (transcriptions_dir / "00001-00001.json").write_text(json.dumps(chapter_one), encoding="utf-8")
    (transcriptions_dir / "00002-00001.json").write_text(json.dumps(chapter_two), encoding="utf-8")

    monkeypatch.setenv("STORYTELLER_ASSETS_DIR", str(assets_root))
    monkeypatch.setenv("DATA_DIR", str(data_dir))

    manifest_path = ingest_storyteller_transcripts("ebook-hash-1", "Book Three", [])
    assert manifest_path is not None

    manifest_payload = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    assert manifest_payload["chapter_count"] == 2
    assert manifest_payload["duration"] == 8.0
    assert [Path(c["file"]).name for c in manifest_payload["chapters"]] == ["00000-00001.json", "00000-00002.json"]
    assert manifest_payload["chapters"][0]["start"] == 0.0
    assert manifest_payload["chapters"][0]["end"] == 5.0
    assert manifest_payload["chapters"][1]["start"] == 5.0
    assert manifest_payload["chapters"][1]["end"] == 8.0
    assert manifest_payload["chapters"][0]["text_len"] > 0
    assert manifest_payload["chapters"][1]["text_len_utf16"] > 0


def test_transcriber_format_dispatch(tmp_path):
    transcriber = AudioTranscriber(tmp_path, MagicMock(), MagicMock())

    storyteller_chapter = {"transcript": "abc", "wordTimeline": []}
    segment_list = [{"start": 0.0, "end": 1.0, "text": "abc"}]

    assert transcriber._detect_transcript_format(storyteller_chapter) == "storyteller_word_timeline"
    assert transcriber._detect_transcript_format(segment_list) == "segment_list"

    manifest_path, _, _ = _write_storyteller_fixture(tmp_path)
    loaded = transcriber._get_cached_transcript(manifest_path)
    assert isinstance(loaded, StorytellerTranscript)


def test_storyteller_transcript_binary_search_methods(tmp_path):
    manifest_path, _, _ = _write_storyteller_fixture(tmp_path)
    transcript = StorytellerTranscript(manifest_path)

    assert transcript.timestamp_to_char_offset(0.7, chapter_index=0) == 0
    assert transcript.timestamp_to_char_offset(1.1, chapter_index=0) == 6

    assert transcript.char_offset_to_timestamp(0, chapter_index=1) == 0.2
    assert transcript.char_offset_to_timestamp(8, chapter_index=1) == 1.2

    text_at_time = transcript.get_text_at_time(10.3)
    assert "second chapter" in text_at_time

    text_at_offset = transcript.get_text_at_character_offset(7, chapter_index=1)
    assert "second chapter" in text_at_offset


def test_storyteller_utf16_to_python_offset_conversion(tmp_path):
    transcript_dir = tmp_path / "storyteller_utf16"
    transcript_dir.mkdir(parents=True, exist_ok=True)

    chapter_name = "00000-00001.json"
    chapter_payload = {
        "transcript": "A🙂B",
        "wordTimeline": [
            {
                "type": "word",
                "text": "A",
                "startTime": 0.1,
                "endTime": 0.2,
                "startOffsetUtf16": 0,
                "endOffsetUtf16": 1,
                "timeline": [],
            },
            {
                "type": "word",
                "text": "🙂",
                "startTime": 0.3,
                "endTime": 0.4,
                "startOffsetUtf16": 1,
                "endOffsetUtf16": 3,
                "timeline": [],
            },
            {
                "type": "word",
                "text": "B",
                "startTime": 0.5,
                "endTime": 0.6,
                "startOffsetUtf16": 3,
                "endOffsetUtf16": 4,
                "timeline": [],
            },
        ],
    }
    (transcript_dir / chapter_name).write_text(json.dumps(chapter_payload), encoding="utf-8")

    manifest = {
        "format": "storyteller_manifest",
        "version": 1,
        "duration": 1.0,
        "chapters": [
            {"index": 0, "file": chapter_name, "start": 0.0, "end": 1.0, "text_len": 3, "text_len_utf16": 4},
        ],
    }
    manifest_path = transcript_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    transcript = StorytellerTranscript(manifest_path)
    assert transcript.chapter_utf16_to_python_offset(0, 1) == 1
    assert transcript.chapter_utf16_to_python_offset(0, 3) == 2

    story_pos = transcript.timestamp_to_story_position(0.35)
    assert story_pos is not None
    assert story_pos["offset_utf16"] == 1
    assert story_pos["offset_py"] == 1
    assert story_pos["global_offset_py"] == 1
