import pytest
from src.utils.polisher import Polisher

@pytest.fixture
def polisher():
    return Polisher()

def test_normalize_basic(polisher):
    assert polisher.normalize("Hello World") == "hello world"
    assert polisher.normalize("  Spaces   ") == "spaces"
    assert polisher.normalize("Punctuation!") == "punctuation"

def test_normalize_roman(polisher):
    # Depending on implementation, checking if roman numerals are handled or ignored
    # Polisher.normalize currently does NOT enforce roman conversion unless enabled
    # But let's check basic text
    assert polisher.normalize("Chapter IV") == "chapter iv" 
    # If using text_to_digits fallback
    assert polisher.normalize("Chapter One") == "chapter 1"

def test_clean_punctuation(polisher):
    assert polisher.clean_punctuation("Mr. Smith's dog") == "Mr Smiths dog"
    assert polisher.clean_punctuation("hello-world_test") == "hello world test"

def test_text_to_digits(polisher):
    assert polisher.text_to_digits("one") == "1"
    assert polisher.text_to_digits("twenty one") == "21"
    assert polisher.text_to_digits("ninety nine") == "99"

def test_rebuild_fragmented_sentences(polisher):
    ebook_text = "First part. Second part."
    # Fragmented inputs
    segments = [
        {'start': 0.0, 'end': 1.0, 'text': "First"},
        {'start': 1.0, 'end': 2.0, 'text': "part."},
        {'start': 2.0, 'end': 3.0, 'text': "Second part."}
    ]
    
    rebuilt = polisher.rebuild_fragmented_sentences(segments, ebook_text)
    
    assert len(rebuilt) == 2
    assert rebuilt[0]['text'] == "First part."
    assert rebuilt[1]['text'] == "Second part."

def test_rebuild_keeps_segments_whose_bounds_were_measured(polisher):
    # A read-along times each clip, so the boundary inside a sentence is a real
    # time; joining the clips would spread the words over the whole sentence.
    segments = [
        {'start': 0.0, 'end': 10.0, 'text': "And then the", 'timed_bounds': True},
        {'start': 10.0, 'end': 100.0, 'text': "government nationalized the mines.", 'timed_bounds': True},
        {'start': 100.0, 'end': 101.0, 'text': "First"},
        {'start': 101.0, 'end': 102.0, 'text': "part."},
    ]

    rebuilt = polisher.rebuild_fragmented_sentences(segments, "")

    assert [(s['start'], s['end']) for s in rebuilt] == [(0.0, 10.0), (10.0, 100.0), (100.0, 102.0)]
