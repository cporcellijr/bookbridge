import json

import pytest

from src.utils.polisher import Polisher
from src.utils.transcriber import AudioTranscriber


def _transcriber(tmp_path):
    return AudioTranscriber(tmp_path, None, Polisher())


def _cache(transcriber, tmp_path, segments, **extra):
    cache_dir = transcriber.cache_root / "book"
    cache_dir.mkdir(parents=True)
    progress = {
        "done": True,
        "chunks_completed": 1,
        "transcript": segments,
        **extra,
    }
    (cache_dir / "_progress.json").write_text(json.dumps(progress), encoding="utf-8")
    return progress


@pytest.mark.parametrize(("text", "tokens"), [
    ("мастер-распорядитель", [" мастер", "-распорядитель"]),
    ("мастер-распорядитель", [" мастер", "-", "распорядитель"]),
    ("мастер-распорядитель", [" мастер-распорядитель"]),
    ("well-known author", [" well-known", " author"]),
    ("well known author", [" well-known author"]),
    ("twenty-one books", [" twenty", "-one", " books"]),
    ("twenty one books", [" twenty one", " books"]),
    ("notebook", [" note", " book"]),
    ("note book", [" notebook"]),
    # Real faster-whisper splits from a live English cache (#527 live verify).
    ("$1,700. Unless Abby", [" $1", ",700.", " Unless", " Abby"]),
    ("He was at 5'10. She had 4.20.", [" He", " was", " at", " 5", "'10.", " She", " had", " 4", ".20."]),
])
def test_completed_cache_accepts_provider_word_boundaries(tmp_path, text, tokens):
    transcriber = _transcriber(tmp_path)
    segments = [{
        "start": 0.0, "end": float(len(tokens)), "text": text,
        "words": [
            {"word": token, "start": float(index), "end": float(index + 1)}
            for index, token in enumerate(tokens)
        ],
    }]
    _cache(transcriber, tmp_path, segments)
    cache_file = transcriber.cache_root / "book" / "_progress.json"
    original = cache_file.read_bytes()

    assert transcriber.has_word_timed_transcript("book")
    assert transcriber.get_cached_word_timed_transcript("book", len(tokens)) == segments
    assert cache_file.read_bytes() == original


@pytest.mark.parametrize(("text", "tokens"), [
    ("мастер-распорядитель", [" мастер"]),
    ("мастер-распорядитель", [" мастер", "-писатель"]),
    ("мастер-распорядитель", [" распорядитель", "-мастер"]),
    ("$1,700. Unless", [" $1", ",900.", " Unless"]),
])
def test_completed_cache_still_requires_complete_ordered_text(tmp_path, text, tokens):
    transcriber = _transcriber(tmp_path)
    segments = [{
        "start": 0.0, "end": float(len(tokens)), "text": text,
        "words": [
            {"word": token, "start": float(index), "end": float(index + 1)}
            for index, token in enumerate(tokens)
        ],
    }]
    _cache(transcriber, tmp_path, segments)

    assert not transcriber.has_word_timed_transcript("book")
    assert transcriber.get_cached_word_timed_transcript("book", len(tokens)) is None


def test_completed_partial_provider_words_do_not_prove_cached_transcript(tmp_path):
    transcriber = _transcriber(tmp_path)
    text = " ".join(f"word{i}" for i in range(198))
    segments = [{
        "start": 0.0,
        "end": 100.0,
        "text": text,
        "words": [{"word": "unrelated", "start": 0.0, "end": 10.0}],
    }]
    _cache(transcriber, tmp_path, segments, vad_filter=True, cumulative_duration=100.0)

    assert transcriber.get_cached_word_timed_transcript("book", 100.0) is None
    assert not transcriber.has_word_timed_transcript("book")


def test_word_timing_must_be_finite_ordered_nonboolean_and_inside_segment(tmp_path):
    transcriber = _transcriber(tmp_path)
    invalid = [
        ("one", [{"word": "one", "start": True, "end": 0.5}]),
        ("one", [{"word": "one", "start": -0.1, "end": 0.5}]),
        ("one", [{"word": "one", "start": 0.0, "end": 1.6}]),
        ("one", [{"word": "one", "start": 1.6, "end": 1.7}]),
        ("one two", [
            {"word": "one", "start": 0.5, "end": 0.7},
            {"word": "two", "start": 0.4, "end": 0.8},
        ]),
    ]
    for text, words in invalid:
        segments = [{"start": 0.0, "end": 1.0, "text": text, "words": words}]
        assert not transcriber._valid_word_timed_segments(segments)


def test_word_timing_accepts_small_overhang_past_its_segment(tmp_path):
    transcriber = _transcriber(tmp_path)
    # Real overhangs from a live English cache (#527 live verify): 0.02 s early, 0.11 s late.
    segments = [
        {"start": 9457.079188, "end": 9461.699188, "text": "Sure was,",
         "words": [{"word": " Sure", "start": 9457.059188, "end": 9457.319188},
                   {"word": " was,", "start": 9457.4, "end": 9457.6}]},
        {"start": 14562.546564, "end": 14563.646564, "text": "Hannah looked down at him.",
         "words": [{"word": " Hannah", "start": 14562.6, "end": 14562.9},
                   {"word": " looked down at", "start": 14562.9, "end": 14563.4},
                   {"word": " him.", "start": 14563.466564, "end": 14563.756564}]},
    ]

    assert transcriber._valid_word_timed_segments(segments)


def test_word_timing_order_cannot_reset_between_segments(tmp_path):
    transcriber = _transcriber(tmp_path)
    segments = [
        {"start": 0.0, "end": 10.0, "text": "first",
         "words": [{"word": "first", "start": 5.0, "end": 7.0}]},
        {"start": 0.0, "end": 10.0, "text": "second",
         "words": [{"word": "second", "start": 4.0, "end": 6.0}]},
    ]

    assert not transcriber._valid_word_timed_segments(segments)


def test_complete_word_timings_accept_punctuation_chunks_and_vad_trailing_silence(tmp_path):
    transcriber = _transcriber(tmp_path)
    segments = [{
        "start": 0.0,
        "end": 6.0,
        "text": "Hello, world!",
        "words": [
            {"word": "Hello,", "start": 0.1, "end": 0.7},
            {"word": "world", "start": 0.5, "end": 1.4},
            {"word": "!", "start": 1.4, "end": 1.4},
        ],
    }, {
        "start": 6.0,
        "end": 8.0,
        "text": "Next.",
        "words": [{"word": "Next.", "start": 6.3, "end": 6.8}],
    }]
    _cache(transcriber, tmp_path, segments, vad_filter=True, cumulative_duration=100.0)

    assert transcriber.get_cached_word_timed_transcript("book", 100.0) == segments
    assert transcriber.has_word_timed_transcript("book")
