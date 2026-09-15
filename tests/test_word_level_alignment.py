"""Issue #426: measured word times survive transcription and EPUB remapping."""

import copy
import json
import logging
from unittest.mock import MagicMock, patch

import pytest

from src.db.database_service import DatabaseService
from src.services.alignment_service import AlignmentService
from src.utils.polisher import Polisher
from src.utils.storyteller_transcript import StorytellerTranscript
from src.utils.transcriber import AudioTranscriber


@pytest.fixture
def alignment(tmp_path):
    db = DatabaseService(str(tmp_path / 'alignment.db'))
    try:
        yield AlignmentService(db, Polisher())
    finally:
        db.db_manager.close()


def timed_segment():
    tokens = [f'token{i}' for i in range(40)]
    # A long pause in the middle exposes even-distribution drift.
    words = [{'word': word, 'start': i + (30 if i >= 10 else 0),
              'end': i + (30 if i >= 10 else 0) + 0.4}
             for i, word in enumerate(tokens)]
    return {'start': 0, 'end': 70, 'text': ' '.join(tokens), 'words': words}


def test_stored_map_uses_measured_times_in_canonical_text(alignment, caplog):
    segment = timed_segment()
    text = '\U0001f4da Front matter\n\n' + segment['text']
    with caplog.at_level(logging.INFO):
        assert alignment.align_and_store('word-test', [segment], text)
    char = text.index('token10 ')
    assert alignment.get_char_for_time('word-test', 40) == char
    assert alignment.get_time_for_text('word-test', '', char_offset_hint=char) == 40
    assert alignment.database_service.get_alignment_total_chars('word-test') == len(text)
    assert 'Word timing: 40 measured, 0 estimated tokens' in caplog.text
    old_segment = {k: v for k, v in segment.items() if k != 'words'}
    assert alignment.align_and_store('segment-test', [old_segment], text)
    assert alignment.get_char_for_time('segment-test', 40) != char


@pytest.mark.parametrize('bad_words', [
    None, [], [{'word': 'token0', 'start': 0, 'end': 1}],
    [{'word': 'token0', 'start': float('nan'), 'end': 1}],
    [{'word': 'token0', 'start': 2, 'end': 1}],
    [{'word': 'token0', 'start': 0, 'end': float('inf')}],
    [{'word': 'token0', 'start': -1, 'end': 1}],
    ['invalid'],
])
def test_invalid_or_incomplete_word_timing_uses_legacy_map(alignment, bad_words):
    segment = timed_segment()
    segment.pop('words')
    expected = alignment._generate_alignment_map([segment], segment['text'])
    segment['words'] = bad_words
    assert alignment._generate_alignment_map([segment], segment['text']) == expected


def test_nonmonotonic_word_timing_uses_legacy_map(alignment):
    segment = timed_segment()
    segment['words'][10]['start'] = 0
    assert alignment._timed_segment_tokens(segment) == []


@pytest.mark.parametrize('timed_first', [True, False])
def test_polishing_keeps_timed_and_untimed_neighbours(timed_first):
    timed = {'start': 0, 'end': 1, 'text': 'hello',
             'words': [{'word': 'hello', 'start': 0.1, 'end': 0.9}]}
    plain = {'start': 1, 'end': 2, 'text': 'world'}
    segments = [timed, plain] if timed_first else [plain, timed]
    original = copy.deepcopy(segments)
    assert Polisher().rebuild_fragmented_sentences(segments, 'hello world') == original
    assert segments == original


def test_word_times_survive_multiple_audio_parts_and_completed_cache(tmp_path):
    transcriber = AudioTranscriber(tmp_path, MagicMock(), Polisher())
    segment = timed_segment()
    provider = MagicMock(supports_raw_audio=True)
    provider.transcribe.return_value = [segment]
    sources = [{'local_path': str(tmp_path / f'part{i}.mp3')} for i in range(2)]
    with patch('src.utils.transcriber.get_transcription_provider', return_value=provider), \
            patch.object(transcriber, 'get_audio_duration', return_value=70):
        result = transcriber.process_audio('parts', sources)
        assert result[1]['words'][10]['start'] == 110
        assert result[1]['words'][10]['end'] == 110.4
        assert segment['words'][10]['start'] == 40
        cache = json.loads((tmp_path / 'audio_cache/parts/_progress.json').read_text())
        assert cache['transcript'] == result
        assert transcriber.process_audio('parts', sources) == result
        assert provider.transcribe.call_count == 2


def test_storyteller_preserves_word_times_and_remaps_utf16_to_epub(alignment, tmp_path):
    chapters = []
    texts = []
    for chapter_index in range(2):
        tokens = [f'chapter{chapter_index}word{i}' for i in range(40)]
        text = '\U0001f4da ' + ' '.join(tokens)
        texts.append(text)
        timeline = []
        for i, token in enumerate(tokens):
            start = text.index(token)
            offset = len(text[:start].encode('utf-16-le')) // 2
            ts = i + (30 if i >= 10 else 0)
            timeline.append({'startOffsetUtf16': offset, 'lengthUtf16': len(token),
                             'startTime': ts, 'endTime': ts + 0.4})
        filename = f'chapter{chapter_index}.json'
        (tmp_path / filename).write_text(json.dumps({'transcript': text, 'wordTimeline': timeline}))
        chapters.append({'index': chapter_index, 'file': filename,
                         'start': chapter_index * 100, 'end': chapter_index * 100 + 70})
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({'format': 'storyteller_manifest', 'chapters': chapters, 'duration': 170}))
    transcript = StorytellerTranscript(manifest)
    canonical = 'Different EPUB front matter\n' + '\n\n'.join(texts)
    assert alignment.align_storyteller_and_store('story', transcript, canonical)
    char = canonical.index('chapter1word10 ')
    assert alignment.get_char_for_time('story', 140) == char
    assert alignment.get_time_for_text('story', '', char_offset_hint=char) == 140
    assert alignment.get_book_duration('story') == 169.4


def test_storyteller_linear_fallback_uses_global_duration(alignment):
    transcript = MagicMock(chapters=[])
    transcript.get_global_duration.return_value = 170
    transcript.get_duration.return_value = 70
    assert alignment.align_storyteller_and_store('fallback', transcript, 'ebook')
    assert alignment.get_book_duration('fallback') == 170
