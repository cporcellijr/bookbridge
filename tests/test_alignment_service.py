import pytest
import json
import logging
import os
import tempfile
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
        return_value=([{'char': 0, 'ts': 0.0}, {'char': 5, 'ts': 1.0}], 'lexical')
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
        bare_dir = assets_root / "assets" / "Trad Wife"
        suffixed_dir = assets_root / "assets" / "Trad Wife [5j7RKcRZ]"
        bare_dir.mkdir(parents=True, exist_ok=True)
        transcriptions_dir = suffixed_dir / "transcriptions"
        transcriptions_dir.mkdir(parents=True, exist_ok=True)
        (transcriptions_dir / "00001-00001.json").write_text(
            json.dumps({"transcript": "hello", "wordTimeline": []}),
            encoding="utf-8",
        )

        result = _resolve_storyteller_title_dir(assets_root, "Trad Wife")

    assert result == suffixed_dir


def test_probe_storyteller_transcripts_uses_suffixed_storyteller_assets_dir():
    with tempfile.TemporaryDirectory() as tmp:
        assets_root = Path(tmp)
        (assets_root / "assets" / "Trad Wife").mkdir(parents=True, exist_ok=True)
        transcriptions_dir = assets_root / "assets" / "Trad Wife [5j7RKcRZ]" / "transcriptions"
        transcriptions_dir.mkdir(parents=True, exist_ok=True)
        for idx in range(2):
            (transcriptions_dir / f"00001-{idx + 1:05d}.json").write_text(
                json.dumps({"transcript": "hello", "wordTimeline": [{"endTime": 1.0}]}),
                encoding="utf-8",
            )

        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("STORYTELLER_ASSETS_DIR", str(assets_root))
            result = probe_storyteller_transcripts(
                "Trad Wife",
                [{"start": 0.0, "end": 1.0}, {"start": 1.0, "end": 2.0}],
            )

    assert result["ready"] is True
    assert result["transcriptions_dir"] == transcriptions_dir


def test_resolve_storyteller_title_dir_matches_title_with_bracket_suffix_when_only_suffixed_dir_exists():
    with tempfile.TemporaryDirectory() as tmp:
        assets_root = Path(tmp)
        suffixed_dir = assets_root / "assets" / "Trad Wife [5j7RKcRZ]"
        suffixed_dir.mkdir(parents=True, exist_ok=True)

        result = _resolve_storyteller_title_dir(assets_root, "Trad Wife")

    assert result == suffixed_dir


def test_resolve_storyteller_title_dir_returns_none_when_multiple_transcript_ready_suffix_variants_exist():
    with tempfile.TemporaryDirectory() as tmp:
        assets_root = Path(tmp)
        first_dir = assets_root / "assets" / "Trad Wife [5j7RKcRZ]"
        second_dir = assets_root / "assets" / "Trad Wife [ABCD1234]"
        for folder in (first_dir, second_dir):
            transcriptions_dir = folder / "transcriptions"
            transcriptions_dir.mkdir(parents=True, exist_ok=True)
            (transcriptions_dir / "00001-00001.json").write_text(
                json.dumps({"transcript": "hello", "wordTimeline": []}),
                encoding="utf-8",
            )

        result = _resolve_storyteller_title_dir(assets_root, "Trad Wife")

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
        alignment_map, method = service._generate_alignment_map_with_method(
            _topic_segments(), _topic_book_text()
        )
    assert method == "llm_anchor"
    assert len(alignment_map) >= 2
    chars = [p["char"] for p in alignment_map]
    assert chars == sorted(chars)  # monotonic in char


def test_anchor_rescue_noop_when_disabled(mock_db):
    service = AlignmentService(mock_db, Polisher(), ollama_client=_TopicOllama())
    with pytest.MonkeyPatch.context() as mp:
        _topic_env(mp)
        mp.setenv("OLLAMA_ALIGN_ANCHOR_RESCUE", "false")
        alignment_map, method = service._generate_alignment_map_with_method(
            _topic_segments(), _topic_book_text()
        )
    assert method == "linear"
    assert alignment_map == [
        {"char": 0, "ts": 0.0},
        {"char": len(_topic_book_text()), "ts": 20.0},
    ]


def test_anchor_rescue_noop_without_client(mock_db):
    service = AlignmentService(mock_db, Polisher(), ollama_client=None)
    with pytest.MonkeyPatch.context() as mp:
        _topic_env(mp)
        _map, method = service._generate_alignment_map_with_method(
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
