import json

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
        ("one", [{"word": "one", "start": 0.0, "end": 1.1}]),
        ("one two", [
            {"word": "one", "start": 0.5, "end": 0.7},
            {"word": "two", "start": 0.4, "end": 0.8},
        ]),
    ]
    for text, words in invalid:
        segments = [{"start": 0.0, "end": 1.0, "text": text, "words": words}]
        assert not transcriber._valid_word_timed_segments(segments)


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
