import pytest
import json
import sys
import types
from unittest.mock import MagicMock
from src.services import alignment_service as alignment_service_module
from src.services.alignment_service import AlignmentService
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
    service._generate_alignment_map = MagicMock(return_value=[{'char': 0, 'ts': 0.0}, {'char': 5, 'ts': 1.0}])
    
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


def test_error_align_anchors_outperform_legacy_ngram(service, monkeypatch):
    # Build long enough corpus for legacy N=12 anchors, but inject transcript errors.
    book_tokens = [
        "alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel",
        "india", "juliet", "kilo", "lima", "mike", "november", "oscar", "papa",
        "quebec", "romeo", "sierra", "tango",
    ]
    transcript_tokens = [
        "alpha", "bravo", "charli", "delta", "echo", "foxtrot", "golf", "EXTRA",
        "hotel", "india", "juliet", "kilo", "lima", "mike", "november", "oscar",
        "papa", "quebec", "romeo", "sierr", "tango",
    ]
    full_text = " ".join(book_tokens)
    segments = [{"start": float(i), "end": float(i + 1), "text": w} for i, w in enumerate(transcript_tokens)]

    # Measure legacy path by forcing error-align path to return None.
    legacy_map = service._generate_alignment_map_error_align
    monkeypatch.setattr(service, "_generate_alignment_map_error_align", lambda *args, **kwargs: None)
    legacy_result = service._generate_alignment_map(segments, full_text)
    legacy_anchor_count = max(0, len(legacy_result) - 2)  # strip forced start/end points

    # Restore method and inject a deterministic fake error_align module.
    monkeypatch.setattr(service, "_generate_alignment_map_error_align", legacy_map)

    class FakeAlignment:
        def __init__(self, typ, ref="", hyp=""):
            self.type = typ
            self.ref = ref
            self.hyp = hyp

    fake_mod = types.ModuleType("error_align")
    fake_mod.error_align = lambda ref, hyp: [
        FakeAlignment("match", "alpha", "alpha"),
        FakeAlignment("match", "bravo", "bravo"),
        FakeAlignment("substitute", "charlie", "charli"),
        FakeAlignment("match", "delta", "delta"),
        FakeAlignment("match", "echo", "echo"),
        FakeAlignment("match", "foxtrot", "foxtrot"),
        FakeAlignment("match", "golf", "golf"),
        FakeAlignment("insert", "", "extra"),
        FakeAlignment("match", "hotel", "hotel"),
        FakeAlignment("match", "india", "india"),
        FakeAlignment("match", "juliet", "juliet"),
        FakeAlignment("match", "kilo", "kilo"),
        FakeAlignment("match", "lima", "lima"),
        FakeAlignment("match", "mike", "mike"),
        FakeAlignment("match", "november", "november"),
        FakeAlignment("match", "oscar", "oscar"),
        FakeAlignment("match", "papa", "papa"),
        FakeAlignment("match", "quebec", "quebec"),
        FakeAlignment("match", "romeo", "romeo"),
        FakeAlignment("substitute", "sierra", "sierr"),
        FakeAlignment("match", "tango", "tango"),
    ]
    monkeypatch.setitem(sys.modules, "error_align", fake_mod)

    improved_map = service._generate_alignment_map(segments, full_text)
    improved_anchor_count = max(0, len(improved_map) - 2)

    assert improved_anchor_count > legacy_anchor_count


def test_word_edit_distance_fallback_matches_expected(monkeypatch):
    # Force fallback DP implementation path (no rapidfuzz).
    monkeypatch.setattr(alignment_service_module, "Levenshtein", None)
    assert AlignmentService._word_edit_distance("wizard", "lizard") == 1
    assert AlignmentService._word_edit_distance("can't", "cant") == 1
    assert AlignmentService._word_edit_distance("going", "gonna") >= 2


def test_error_align_low_density_rejected(service, monkeypatch):
    # Large token streams but only a handful of matches should fail density gating.
    book_tokens = [f"token{i}" for i in range(1200)]
    transcript_tokens = list(book_tokens)
    full_text = " ".join(book_tokens)
    segments = [{"start": float(i), "end": float(i + 1), "text": w} for i, w in enumerate(transcript_tokens)]

    class FakeAlignment:
        def __init__(self, typ):
            self.type = typ

    sparse = [FakeAlignment("match") for _ in range(6)] + [FakeAlignment("insert") for _ in range(1194)]
    fake_mod = types.ModuleType("error_align")
    fake_mod.error_align = lambda ref, hyp: sparse
    monkeypatch.setitem(sys.modules, "error_align", fake_mod)

    # Directly call new method: low token-anchor ratio should reject.
    transcript_words = [{"word": w, "ts": float(i), "orig_index": i} for i, w in enumerate(transcript_tokens)]
    book_words = [{"word": w, "char": i * 7, "orig_index": i} for i, w in enumerate(book_tokens)]
    result = service._generate_alignment_map_error_align(transcript_words, book_words, segments, full_text)
    assert result is None
