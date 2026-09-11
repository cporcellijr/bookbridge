import pytest
import json
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock
from src.services.alignment_service import (
    AlignmentService,
    _resolve_storyteller_title_dir,
    probe_storyteller_transcripts,
)
from src.utils.polisher import Polisher
from src.db.models import BookAlignment

@pytest.fixture
def mock_db():
    db = MagicMock()
    session = MagicMock()
    db.get_session.return_value = session
    # Most stored maps predate the total_chars column; model that explicitly so
    # tests exercise the documented fallback instead of a MagicMock's __int__.
    db.get_alignment_total_chars.return_value = None
    return db

@pytest.fixture
def service(mock_db):
    return AlignmentService(mock_db, Polisher())

def test_align_and_store_success(service, mock_db):
    ebook_text = "Alice in Wonderland"
    segments = [{'start': 0.0, 'end': 1.0, 'text': "Alice"}]
    
    # Setup Session Context
    session = mock_db.get_session()
    session.__enter__.return_value = session
    
    # Mock lower-level alignment logic (tested separately in test_generate_alignment_map)
    # We only want to verify the storage flow here
    service._generate_alignment_map_with_method = MagicMock(
        return_value=([{'char': 0, 'ts': 0.0}, {'char': 5, 'ts': 1.0}], 'lexical', None)
    )

    # Ensure DB query returns None (Simulate no existing record)
    session.query.return_value.filter_by.return_value.first.return_value = None

    result = service.align_and_store("test_id", segments, ebook_text)

    assert result == True
    session.add.assert_called()

def test_generate_alignment_map(service):
    ebook_text = "One two three four five."
    segments = [
        {'start': 0.0, 'end': 1.0, 'text': "One two"},
        {'start': 1.0, 'end': 2.0, 'text': "three four"},
        {'start': 2.0, 'end': 3.0, 'text': "five"}
    ]
    
    # N=12 in implementation is large, so with short text it might fail finding anchors?
    # Actually, N=12 refers to N-grams of WORDS? 
    # Code: keys = [x['word'] for x in items[i:i+N]] -> Yes, 12 words.
    # So short text won't align with N=12.
    # We need longer text for this test or need to mock the constant.
    
    # Let's mock the N constant or provide long text?
    # Providing long text is safer.
    
    tokens = ["word" + str(i) for i in range(20)]
    ebook_text = " ".join(tokens)
    
    # Create segments roughly matching
    segments = []
    for i in range(20):
        segments.append({'start': float(i), 'end': float(i+1), 'text': tokens[i]})
        
    alignment_map = service._generate_alignment_map(segments, ebook_text)
    
    assert len(alignment_map) > 0
    # Should contain start (0,0) and likely some anchors
    assert alignment_map[0]['char'] == 0
    assert alignment_map[0]['ts'] == 0.0

def _stub_alignment_row(mock_db, alignment_map):
    session = mock_db.get_session()
    session.__enter__.return_value = session
    if alignment_map is None:
        session.query.return_value.filter_by.return_value.first.return_value = None
    else:
        entry = MagicMock()
        entry.alignment_map_json = json.dumps(alignment_map)
        session.query.return_value.filter_by.return_value.first.return_value = entry
    return session


def test_get_alignment_caches_parsed_map(service, mock_db):
    mock_map = [{'char': 0, 'ts': 0.0}, {'char': 100, 'ts': 10.0}]
    _stub_alignment_row(mock_db, mock_map)
    mock_db.get_session.reset_mock()

    first = service._get_alignment("test_id")
    second = service._get_alignment("test_id")

    assert first == mock_map
    assert second is first  # served from cache, no re-parse
    assert mock_db.get_session.call_count == 1  # DB hit only on the first call


def test_save_alignment_invalidates_cache(service, mock_db):
    stale_map = [{'char': 0, 'ts': 0.0}]
    fresh_map = [{'char': 0, 'ts': 0.0}, {'char': 50, 'ts': 5.0}]
    session = _stub_alignment_row(mock_db, stale_map)

    assert service._get_alignment("test_id") == stale_map

    session.query.return_value.filter_by.return_value.first.return_value = None
    service._save_alignment("test_id", fresh_map, align_method="lexical")

    _stub_alignment_row(mock_db, fresh_map)
    assert service._get_alignment("test_id") == fresh_map


def test_get_alignment_missing_row_is_not_cached(service, mock_db):
    _stub_alignment_row(mock_db, None)
    assert service._get_alignment("test_id") is None

    mock_map = [{'char': 0, 'ts': 0.0}]
    _stub_alignment_row(mock_db, mock_map)
    assert service._get_alignment("test_id") == mock_map


def test_get_time_for_text(service, mock_db):
    # Mock _get_alignment return
    mock_map = [
        {'char': 0, 'ts': 0.0},
        {'char': 100, 'ts': 10.0}
    ]
    
    session = mock_db.get_session()
    session.__enter__.return_value = session
    mock_entry = MagicMock()
    mock_entry.alignment_map_json = json.dumps(mock_map)
    session.query.return_value.filter_by.return_value.first.return_value = mock_entry
    
    # Test Exact
    ts = service.get_time_for_text("test_id", "query", char_offset_hint=0)
    assert ts == 0.0
    
    # Test Interpolation (50 chars -> 5.0s)
    ts = service.get_time_for_text("test_id", "query", char_offset_hint=50)
    assert ts == 5.0


def test_get_progress_for_time_maps_audio_ts_to_text_fraction(service):
    # Deliberately non-linear: half the audio time (5.0s) is 60% of the text.
    # This is the audio-time vs ebook-text axis mismatch the dashboard warning
    # must account for.
    service._get_alignment = MagicMock(return_value=[
        {'char': 0, 'ts': 0.0},
        {'char': 600, 'ts': 5.0},
        {'char': 1000, 'ts': 10.0},
    ])

    assert service.get_progress_for_time("id", 5.0) == pytest.approx(0.60)
    # Interpolated: ts 2.5 -> char 300 -> 0.30
    assert service.get_progress_for_time("id", 2.5) == pytest.approx(0.30)


def test_get_progress_for_time_clamps_to_bounds(service):
    service._get_alignment = MagicMock(return_value=[
        {'char': 0, 'ts': 0.0},
        {'char': 1000, 'ts': 10.0},
    ])

    assert service.get_progress_for_time("id", 999.0) == pytest.approx(1.0)
    assert service.get_progress_for_time("id", -5.0) == pytest.approx(0.0)


def test_get_progress_for_time_returns_none_without_alignment(service):
    service._get_alignment = MagicMock(return_value=None)
    assert service.get_progress_for_time("id", 5.0) is None


def test_probe_storyteller_transcripts_returns_ready_when_assets_not_configured():
    with pytest.MonkeyPatch.context() as mp:
        mp.delenv("STORYTELLER_ASSETS_DIR", raising=False)
        result = probe_storyteller_transcripts("Auto Book", [])

    assert result["ready"] is True
    assert result["reason"] == "assets_not_configured"


def test_probe_storyteller_transcripts_returns_not_ready_when_transcriptions_dir_missing():
    with tempfile.TemporaryDirectory() as tmp:
        assets_root = Path(tmp)
        (assets_root / "assets" / "Auto Book").mkdir(parents=True, exist_ok=True)

        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("STORYTELLER_ASSETS_DIR", str(assets_root))
            result = probe_storyteller_transcripts("Auto Book", [{"start": 0.0, "end": 1.0}])

    assert result["ready"] is False
    assert result["reason"] == "transcriptions_dir_missing"


def test_probe_storyteller_transcripts_logs_search_root_and_available_dirs_on_title_dir_missing(caplog):
    with tempfile.TemporaryDirectory() as tmp:
        assets_root = Path(tmp)
        assets_dir = assets_root / "assets"
        (assets_dir / "Dune").mkdir(parents=True, exist_ok=True)
        (assets_dir / "Foundation [ABC123]").mkdir(parents=True, exist_ok=True)

        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("STORYTELLER_ASSETS_DIR", str(assets_root))
            with caplog.at_level(logging.INFO):
                result = probe_storyteller_transcripts(
                    "The Fellowship of the Ring",
                    [{"start": 0.0, "end": 1.0}],
                    storyteller_title="The Fellowship of the Ring [XYZ789]",
                )

    assert result["ready"] is False
    assert result["reason"] == "title_dir_missing"
    assert str(assets_dir) in caplog.text
    assert "exists=True" in caplog.text
    assert "is_dir=True" in caplog.text
    assert "Dune" in caplog.text
    assert "Foundation [ABC123]" in caplog.text
    assert "The Fellowship of the Ring" in caplog.text


def test_probe_storyteller_transcripts_accepts_count_mismatch_with_audio_aligned():
    # 1 valid file with 2 ABS chapters — validation now accepts the found count
    # and flags audio_aligned=True so ingest derives timing from file contents.
    with tempfile.TemporaryDirectory() as tmp:
        assets_root = Path(tmp)
        transcriptions_dir = assets_root / "assets" / "Auto Book" / "transcriptions"
        transcriptions_dir.mkdir(parents=True, exist_ok=True)
        (transcriptions_dir / "00000-00001.json").write_text(
            json.dumps({"transcript": "hello", "wordTimeline": [{"endTime": 5.0}]}),
            encoding="utf-8",
        )

        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("STORYTELLER_ASSETS_DIR", str(assets_root))
            result = probe_storyteller_transcripts(
                "Auto Book",
                [{"start": 0.0, "end": 1.0}, {"start": 1.0, "end": 2.0}],
            )

    assert result["ready"] is True
    assert result["reason"] == "validated"
    assert result["audio_aligned"] is True


def test_probe_storyteller_transcripts_returns_ready_when_validated():
    with tempfile.TemporaryDirectory() as tmp:
        assets_root = Path(tmp)
        transcriptions_dir = assets_root / "assets" / "Auto Book" / "transcriptions"
        transcriptions_dir.mkdir(parents=True, exist_ok=True)
        for idx in range(2):
            (transcriptions_dir / f"00000-{idx + 1:05d}.json").write_text(
                json.dumps({"transcript": "hello", "wordTimeline": [{"endTime": 1.0}]}),
                encoding="utf-8",
            )

        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("STORYTELLER_ASSETS_DIR", str(assets_root))
            result = probe_storyteller_transcripts(
                "Auto Book",
                [{"start": 0.0, "end": 1.0}, {"start": 1.0, "end": 2.0}],
            )

    assert result["ready"] is True
    assert result["reason"] == "validated"


def test_resolve_storyteller_title_dir_prefers_suffixed_dir_with_transcriptions_over_bare_dir_without_transcriptions():
    with tempfile.TemporaryDirectory() as tmp:
        assets_root = Path(tmp)
        bare_dir = assets_root / "assets" / "Home Maker"
        suffixed_dir = assets_root / "assets" / "Home Maker [5j7RKcRZ]"
        bare_dir.mkdir(parents=True, exist_ok=True)
        transcriptions_dir = suffixed_dir / "transcriptions"
        transcriptions_dir.mkdir(parents=True, exist_ok=True)
        (transcriptions_dir / "00001-00001.json").write_text(
            json.dumps({"transcript": "hello", "wordTimeline": []}),
            encoding="utf-8",
        )

        result = _resolve_storyteller_title_dir(assets_root, "Home Maker")

    assert result == suffixed_dir


def test_probe_storyteller_transcripts_uses_suffixed_storyteller_assets_dir():
    with tempfile.TemporaryDirectory() as tmp:
        assets_root = Path(tmp)
        (assets_root / "assets" / "Home Maker").mkdir(parents=True, exist_ok=True)
        transcriptions_dir = assets_root / "assets" / "Home Maker [5j7RKcRZ]" / "transcriptions"
        transcriptions_dir.mkdir(parents=True, exist_ok=True)
        for idx in range(2):
            (transcriptions_dir / f"00001-{idx + 1:05d}.json").write_text(
                json.dumps({"transcript": "hello", "wordTimeline": [{"endTime": 1.0}]}),
                encoding="utf-8",
            )

        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("STORYTELLER_ASSETS_DIR", str(assets_root))
            result = probe_storyteller_transcripts(
                "Home Maker",
                [{"start": 0.0, "end": 1.0}, {"start": 1.0, "end": 2.0}],
            )

    assert result["ready"] is True
    assert result["transcriptions_dir"] == transcriptions_dir


def test_resolve_storyteller_title_dir_matches_title_with_bracket_suffix_when_only_suffixed_dir_exists():
    with tempfile.TemporaryDirectory() as tmp:
        assets_root = Path(tmp)
        suffixed_dir = assets_root / "assets" / "Home Maker [5j7RKcRZ]"
        suffixed_dir.mkdir(parents=True, exist_ok=True)

        result = _resolve_storyteller_title_dir(assets_root, "Home Maker")

    assert result == suffixed_dir


def test_resolve_storyteller_title_dir_returns_none_when_multiple_transcript_ready_suffix_variants_exist():
    with tempfile.TemporaryDirectory() as tmp:
        assets_root = Path(tmp)
        first_dir = assets_root / "assets" / "Home Maker [5j7RKcRZ]"
        second_dir = assets_root / "assets" / "Home Maker [ABCD1234]"
        for folder in (first_dir, second_dir):
            transcriptions_dir = folder / "transcriptions"
            transcriptions_dir.mkdir(parents=True, exist_ok=True)
            (transcriptions_dir / "00001-00001.json").write_text(
                json.dumps({"transcript": "hello", "wordTimeline": []}),
                encoding="utf-8",
            )

        result = _resolve_storyteller_title_dir(assets_root, "Home Maker")

    assert result is None


# ---------------------------------------------------------------------------
# Track C: embedding anchor rescue + content-match guard
# ---------------------------------------------------------------------------

class _TopicOllama:
    """Stub OllamaClient: embeds by topic keyword so tests can craft matches.

    Returns [1,0] for 'ocean' text, [0,1] for 'mountain' text, else [0.5,0.5].
    """

    def __init__(self, configured=True):
        self._configured = configured

    def is_configured(self):
        return self._configured

    def embed(self, texts):
        out = []
        for t in texts:
            low = (t or "").lower()
            if "ocean" in low:
                out.append([1.0, 0.0])
            elif "mountain" in low:
                out.append([0.0, 1.0])
            else:
                out.append([0.5, 0.5])
        return out


def _topic_env(mp):
    mp.setenv("OLLAMA_ALIGN_ANCHOR_RESCUE", "true")
    mp.setenv("OLLAMA_ALIGN_CONTENT_GUARD", "true")
    mp.setenv("OLLAMA_ALIGN_SIM_THRESHOLD", "0.72")
    mp.setenv("OLLAMA_ALIGN_MAX_WINDOWS", "80")
    mp.setenv("OLLAMA_ALIGN_CONTENT_MIN_SIM", "0.45")


def _topic_book_text():
    # Two distinct topical halves; no shared 12-gram with the transcript -> lexical fails.
    return ("ocean " * 60).strip() + " " + ("mountain " * 60).strip()


def _topic_segments():
    return [
        {"start": 0.0, "end": 10.0, "text": "the sea waves ocean tide rolling"},
        {"start": 10.0, "end": 20.0, "text": "the peak summit mountain ridge climbing"},
    ]


def test_anchor_rescue_builds_map_when_lexical_fails(mock_db):
    service = AlignmentService(mock_db, Polisher(), ollama_client=_TopicOllama())
    with pytest.MonkeyPatch.context() as mp:
        _topic_env(mp)
        alignment_map, method, map_segments = service._generate_alignment_map_with_method(
            _topic_segments(), _topic_book_text()
        )
    assert method == "llm_anchor"
    assert map_segments is None
    assert len(alignment_map) >= 2
    chars = [p["char"] for p in alignment_map]
    assert chars == sorted(chars)  # monotonic in char


def test_anchor_rescue_noop_when_disabled(mock_db):
    service = AlignmentService(mock_db, Polisher(), ollama_client=_TopicOllama())
    with pytest.MonkeyPatch.context() as mp:
        _topic_env(mp)
        mp.setenv("OLLAMA_ALIGN_ANCHOR_RESCUE", "false")
        alignment_map, method, map_segments = service._generate_alignment_map_with_method(
            _topic_segments(), _topic_book_text()
        )
    assert method == "linear"
    assert map_segments is None
    assert alignment_map == [
        {"char": 0, "ts": 0.0},
        {"char": len(_topic_book_text()), "ts": 20.0},
    ]


class _RecordingTopicOllama(_TopicOllama):
    """Topic stub that also records every text passed to embed()."""

    def __init__(self):
        super().__init__()
        self.embedded_texts = []

    def embed(self, texts):
        self.embedded_texts.extend(texts)
        return super().embed(texts)


def test_anchor_rescue_caps_embedded_window_length(mock_db):
    # Long books produce windows beyond the embedder's token limit; only a
    # bounded prefix may be sent while anchor char offsets still span the book.
    stub = _RecordingTopicOllama()
    service = AlignmentService(mock_db, Polisher(), ollama_client=stub)
    half = 250_000
    long_text = ("ocean " * (half // 6)) + ("mountain " * (half // 9))
    with pytest.MonkeyPatch.context() as mp:
        _topic_env(mp)
        mp.setenv("OLLAMA_ALIGN_CONTENT_GUARD", "false")
        alignment_map, method, _map_segments = service._generate_alignment_map_with_method(
            _topic_segments(), long_text
        )
    assert method == "llm_anchor"
    cap = AlignmentService._EMBED_WINDOW_MAX_CHARS
    assert all(len(t) <= cap for t in stub.embedded_texts)
    assert alignment_map[-1]["char"] == len(long_text)


def test_anchor_rescue_short_book_texts_unchanged(mock_db):
    stub = _RecordingTopicOllama()
    service = AlignmentService(mock_db, Polisher(), ollama_client=stub)
    with pytest.MonkeyPatch.context() as mp:
        _topic_env(mp)
        mp.setenv("OLLAMA_ALIGN_CONTENT_GUARD", "false")
        service._generate_alignment_map_with_method(_topic_segments(), _topic_book_text())
    cap = AlignmentService._EMBED_WINDOW_MAX_CHARS
    # Short-book windows are far below the cap, so nothing is truncated.
    assert stub.embedded_texts
    assert all(len(t) < cap for t in stub.embedded_texts)


def test_anchor_rescue_noop_without_client(mock_db):
    service = AlignmentService(mock_db, Polisher(), ollama_client=None)
    with pytest.MonkeyPatch.context() as mp:
        _topic_env(mp)
        _map, method, _map_segments = service._generate_alignment_map_with_method(
            _topic_segments(), _topic_book_text()
        )
    assert method == "linear"


def test_content_guard_blocks_divergent_content(mock_db):
    service = AlignmentService(mock_db, Polisher(), ollama_client=_TopicOllama())
    segments = [{"start": 0.0, "end": 5.0, "text": "the ocean sea waves"}]
    with pytest.MonkeyPatch.context() as mp:
        _topic_env(mp)
        ok = service._verify_content_match(segments, "mountain " * 100, abs_id="x")
    assert ok is False


def test_content_guard_allows_matching_content(mock_db):
    service = AlignmentService(mock_db, Polisher(), ollama_client=_TopicOllama())
    segments = [{"start": 0.0, "end": 5.0, "text": "the ocean sea waves"}]
    with pytest.MonkeyPatch.context() as mp:
        _topic_env(mp)
        ok = service._verify_content_match(segments, "ocean " * 100, abs_id="x")
    assert ok is True


def test_content_guard_noop_when_disabled(mock_db):
    service = AlignmentService(mock_db, Polisher(), ollama_client=_TopicOllama())
    segments = [{"start": 0.0, "end": 5.0, "text": "the ocean sea waves"}]
    with pytest.MonkeyPatch.context() as mp:
        _topic_env(mp)
        mp.setenv("OLLAMA_ALIGN_CONTENT_GUARD", "false")
        ok = service._verify_content_match(segments, "mountain " * 100, abs_id="x")
    assert ok is True  # guard off -> never blocks


def test_content_guard_noop_without_client(mock_db):
    service = AlignmentService(mock_db, Polisher(), ollama_client=None)
    segments = [{"start": 0.0, "end": 5.0, "text": "the ocean sea waves"}]
    with pytest.MonkeyPatch.context() as mp:
        _topic_env(mp)
        ok = service._verify_content_match(segments, "mountain " * 100, abs_id="x")
    assert ok is True


# --- issue #362b: text-progress denominator ---------------------------------
#
# get_progress_for_time() divided the interpolated character offset by the map's
# LAST ANCHOR character. That anchor is where the transcript stopped matching the
# book, not the end of the book, so every audio position read high — by ~1/coverage.
# On the reporter's 75.5%-coverage map that turned a true 50% into 66.3%.


def _progress_service(mock_db, alignment_map, total_chars):
    service = AlignmentService(mock_db, Polisher())
    service._get_alignment = MagicMock(return_value=alignment_map)
    mock_db.get_alignment_total_chars.return_value = total_chars
    return service


def test_progress_for_time_uses_recorded_ebook_length(mock_db):
    """A map whose anchors stop at 75.5% of the text must not inflate positions."""
    # Anchors span 0 -> 75_470 chars over 0 -> 76_726.9s; the book is 100_000 chars.
    alignment_map = [{"char": 0, "ts": 0.0}, {"char": 75_470, "ts": 76_726.9}]
    service = _progress_service(mock_db, alignment_map, 100_000)

    fraction = service.get_progress_for_time("abs-1", 50_889.5)

    # 50_889.5/76_726.9 * 75_470 = 50_055 chars -> 50.06% of the real book.
    assert fraction == pytest.approx(0.5006, abs=0.001)


def test_progress_for_time_without_total_chars_keeps_legacy_behavior(mock_db):
    """Maps stored before total_chars existed must not change behaviour."""
    alignment_map = [{"char": 0, "ts": 0.0}, {"char": 75_470, "ts": 76_726.9}]
    service = _progress_service(mock_db, alignment_map, None)

    fraction = service.get_progress_for_time("abs-1", 50_889.5)

    # The old denominator (last anchor) — the 1.325x inflation the reporter saw.
    assert fraction == pytest.approx(0.6632, abs=0.001)


def test_progress_for_time_is_clamped(mock_db):
    alignment_map = [{"char": 0, "ts": 0.0}, {"char": 100_000, "ts": 1_000.0}]
    service = _progress_service(mock_db, alignment_map, 100_000)

    assert service.get_progress_for_time("abs-1", 0.0) == pytest.approx(0.0)
    assert service.get_progress_for_time("abs-1", 5_000.0) == pytest.approx(1.0)


def test_progress_for_time_caches_total_chars_lookup(mock_db):
    alignment_map = [{"char": 0, "ts": 0.0}, {"char": 100_000, "ts": 1_000.0}]
    service = _progress_service(mock_db, alignment_map, 100_000)

    service.get_progress_for_time("abs-1", 100.0)
    service.get_progress_for_time("abs-1", 200.0)

    assert mock_db.get_alignment_total_chars.call_count == 1


# --- backfilling total_chars onto pre-existing maps --------------------------
#
# Every map stored before the column existed keeps dividing by its own last
# anchor, and only a re-align would ever record a length. The sync path already
# holds the parsed ebook text, so it can fill them in as books are touched.


def test_backfill_records_length_and_fixes_the_denominator(mock_db):
    alignment_map = [{"char": 0, "ts": 0.0}, {"char": 75_470, "ts": 76_726.9}]
    service = _progress_service(mock_db, alignment_map, None)
    mock_db.set_alignment_total_chars_if_missing.return_value = True

    # Pre-backfill: the legacy last-anchor denominator inflates the position.
    assert service.get_progress_for_time("abs-1", 50_889.5) == pytest.approx(0.6632, abs=0.001)

    assert service.record_total_chars_if_missing("abs-1", 100_000) is True
    mock_db.set_alignment_total_chars_if_missing.assert_called_once_with("abs-1", 100_000)

    # Post-backfill the same timestamp resolves against the real book length,
    # without re-reading the database.
    assert service.get_progress_for_time("abs-1", 50_889.5) == pytest.approx(0.5006, abs=0.001)


def test_backfill_never_overwrites_a_recorded_length(mock_db):
    alignment_map = [{"char": 0, "ts": 0.0}, {"char": 75_470, "ts": 76_726.9}]
    service = _progress_service(mock_db, alignment_map, 100_000)

    # Prime the cache with the recorded value, as a real read would.
    service.get_progress_for_time("abs-1", 1.0)

    assert service.record_total_chars_if_missing("abs-1", 42) is False
    mock_db.set_alignment_total_chars_if_missing.assert_not_called()


def test_backfill_ignores_meaningless_lengths(mock_db):
    service = _progress_service(mock_db, [{"char": 0, "ts": 0.0}], None)

    assert service.record_total_chars_if_missing("abs-1", 0) is False
    assert service.record_total_chars_if_missing("abs-1", -5) is False
    assert service.record_total_chars_if_missing("", 100) is False
    mock_db.set_alignment_total_chars_if_missing.assert_not_called()


def test_backfill_survives_a_database_error(mock_db):
    service = _progress_service(mock_db, [{"char": 0, "ts": 0.0}], None)
    mock_db.set_alignment_total_chars_if_missing.side_effect = RuntimeError("locked")

    assert service.record_total_chars_if_missing("abs-1", 100_000) is False


def test_align_and_store_records_ebook_length(mock_db):
    ebook_text = "Alice in Wonderland"
    session = mock_db.get_session()
    session.__enter__.return_value = session
    session.query.return_value.filter_by.return_value.first.return_value = None

    service = AlignmentService(mock_db, Polisher())
    service._generate_alignment_map_with_method = MagicMock(
        return_value=([{'char': 0, 'ts': 0.0}, {'char': 5, 'ts': 1.0}], 'lexical', None)
    )

    assert service.align_and_store("test_id", [{'start': 0.0, 'end': 1.0, 'text': "Alice"}], ebook_text)

    stored = session.add.call_args[0][0]
    assert stored.total_chars == len(ebook_text)


def test_save_alignment_updates_total_chars_on_existing_row(mock_db):
    session = mock_db.get_session()
    session.__enter__.return_value = session
    existing = BookAlignment(abs_id="test_id", alignment_map_json="[]")
    session.query.return_value.filter_by.return_value.first.return_value = existing

    service = AlignmentService(mock_db, Polisher())
    service._save_alignment("test_id", [{"char": 0, "ts": 0.0}], "lexical", total_chars=4242)

    assert existing.total_chars == 4242


def test_save_alignment_preserves_total_chars_when_not_supplied(mock_db):
    """When updating an existing row without total_chars, the previous value must not be overwritten with None."""
    session = mock_db.get_session()
    session.__enter__.return_value = session
    existing = BookAlignment(abs_id="test_id", alignment_map_json="[]", total_chars=50000)
    session.query.return_value.filter_by.return_value.first.return_value = existing

    service = AlignmentService(mock_db, Polisher())
    # Simulate unanchored Storyteller path that cannot supply total_chars
    service._save_alignment("test_id", [{"char": 0, "ts": 0.0}], "storyteller")

    # The original total_chars must be preserved
    assert existing.total_chars == 50000


def test_progress_for_time_ignores_total_chars_smaller_than_the_map(mock_db):
    """An inconsistent record must not shrink the denominator below the anchors."""
    alignment_map = [{"char": 0, "ts": 0.0}, {"char": 1000, "ts": 10.0}]
    service = _progress_service(mock_db, alignment_map, 10)

    assert service.get_progress_for_time("abs-1", 5.0) == pytest.approx(0.5)


def test_backfill_persists_against_a_real_database():
    """End-to-end: a stored map with no length gets one, and only once."""
    import shutil
    from src.db.database_service import DatabaseService

    tmp = tempfile.mkdtemp()
    try:
        db = DatabaseService(str(Path(tmp) / "align.db"))
        service = AlignmentService(db, Polisher())

        # A map exactly as the pre-column code stored it: no total_chars.
        service._save_alignment("abs-legacy", [{"char": 0, "ts": 0.0},
                                               {"char": 75_470, "ts": 76_726.9}])
        assert db.get_alignment_total_chars("abs-legacy") is None

        assert service.record_total_chars_if_missing("abs-legacy", 100_000) is True
        assert db.get_alignment_total_chars("abs-legacy") == 100_000

        # Second pass is a no-op, and a different length cannot displace it.
        assert service.record_total_chars_if_missing("abs-legacy", 5) is False
        assert db.get_alignment_total_chars("abs-legacy") == 100_000

        # A book with no alignment row at all is simply skipped.
        assert service.record_total_chars_if_missing("abs-unknown", 100_000) is False
    finally:
        if hasattr(db, 'db_manager'):
            db.db_manager.close()
        shutil.rmtree(tmp, ignore_errors=True)


def test_backfill_total_chars_does_not_disturb_last_updated():
    """set_alignment_total_chars_if_missing is a metadata backfill (it only
    fills a NULL total_chars, never rebuilds the map), so it must not stamp
    BookAlignment.last_updated to "now" via onupdate=utcnow. Fails against a
    plain `row.total_chars = ...` ORM assignment, which fires onupdate on the
    resulting UPDATE regardless of whether last_updated itself changed."""
    import shutil
    from src.db.database_service import DatabaseService

    tmp = tempfile.mkdtemp()
    try:
        db = DatabaseService(str(Path(tmp) / "align_total_chars.db"))
        old_stamp = datetime(2020, 1, 1)
        with db.get_session() as session:
            row = BookAlignment(
                abs_id="abs-legacy",
                alignment_map_json=json.dumps(
                    [{"char": 0, "ts": 0.0}, {"char": 75_470, "ts": 76_726.9}]
                ),
            )
            row.last_updated = old_stamp
            session.add(row)

        assert db.set_alignment_total_chars_if_missing("abs-legacy", 100_000) is True

        with db.get_session() as session:
            after = session.query(BookAlignment).filter_by(abs_id="abs-legacy").first()
            assert after.total_chars == 100_000
            assert after.last_updated == old_stamp
    finally:
        if hasattr(db, 'db_manager'):
            db.db_manager.close()
        shutil.rmtree(tmp, ignore_errors=True)
