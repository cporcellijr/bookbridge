from unittest.mock import patch
from zipfile import ZipFile

from src.utils.epub_readalong import audio_matches_single_embedded_track, embedded_audio

_CONTAINER = '<container><rootfiles><rootfile full-path="OPS/book.opf"/></rootfiles></container>'


def _epub(path, *, overlay_type="application/smil+xml", audio=b"audio1", overlay=True):
    media = ' media-overlay="overlay"' if overlay else ''
    with ZipFile(path, 'w') as archive:
        archive.writestr('META-INF/container.xml', _CONTAINER)
        archive.writestr('OPS/book.opf', f'''<package xmlns="http://www.idpf.org/2007/opf"><manifest>
          <item id="text" href="chapter.xhtml"{media}/>
          <item id="overlay" href="chapter.smil" media-type="{overlay_type}"/>
          </manifest><spine><itemref idref="text"/></spine></package>''')
        archive.writestr('OPS/chapter.xhtml', '<html><body><p id="p1">Hello there reader.</p></body></html>')
        archive.writestr('OPS/chapter.smil', '<smil><body><par><text src="chapter.xhtml#p1"/>'
                                             '<audio src="../audio/a%20b.m4a" clipBegin="10s" clipEnd="12s"/>'
                                             '</par></body></smil>')
        archive.writestr('audio/a b.m4a', audio)
    return path


def test_embedded_audio_identifies_exact_track_and_rejects_different_bytes(tmp_path):
    epub = _epub(tmp_path / 'book.epub')
    track = tmp_path / 'track.m4a'
    track.write_bytes(b'audio1')
    assert embedded_audio(epub) == ('audio/a b.m4a',)
    assert audio_matches_single_embedded_track(epub, track)
    track.write_bytes(b'audio2')
    assert not audio_matches_single_embedded_track(epub, track)


def test_embedded_audio_rejects_epubs_without_a_playable_overlay(tmp_path):
    assert embedded_audio(_epub(tmp_path / 'plain.epub', overlay=False)) == ()
    assert embedded_audio(_epub(tmp_path / 'empty.epub', audio=b'')) == ()
    assert embedded_audio(_epub(tmp_path / 'odd.epub', overlay_type='text/plain')) == ()
    assert embedded_audio(tmp_path / 'missing.epub') == ()


def test_verified_single_track_reads_smil_times_as_the_audiobook_clock(tmp_path):
    # #623: a byte-identical single track shares the audiobook's clock, so no
    # per-file offset or timestamp-mode guess may move the SMIL times.
    from src.utils.smil_extractor import SmilExtractor

    epub = _epub(tmp_path / 'book.epub')
    extractor = SmilExtractor()
    with patch.object(SmilExtractor, '_audio_file_offsets', side_effect=AssertionError('offset guess')), \
         patch.object(SmilExtractor, '_detect_timestamp_mode', side_effect=AssertionError('mode guess')):
        segments = extractor.extract_transcript(str(epub), [{'start': 500.0, 'end': 900.0}],
                                                verified_single_track=True)
    assert segments and segments[0]['start'] == 10.0
