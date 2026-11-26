from zipfile import ZipFile

from src.utils.verified_smil_audio import audio_matches_single_embedded_track, embedded_audio


def test_embedded_audio_identifies_exact_track_and_rejects_different_bytes(tmp_path):
    epub = tmp_path / 'book.epub'
    with ZipFile(epub, 'w') as archive:
        archive.writestr('META-INF/container.xml', '<container><rootfiles><rootfile full-path="OPS/book.opf"/></rootfiles></container>')
        archive.writestr('OPS/book.opf', '''<package xmlns="http://www.idpf.org/2007/opf"><manifest>
          <item id="text" href="chapter.xhtml" media-overlay="overlay"/>
          <item id="overlay" href="chapter.smil" media-type="application/smil+xml"/>
          </manifest><spine><itemref idref="text"/></spine></package>''')
        archive.writestr('OPS/chapter.smil', '<smil><body><par><audio src="../audio/a%20b.m4a"/></par></body></smil>')
        archive.writestr('audio/a b.m4a', b'audio1')
    track = tmp_path / 'track.m4a'
    track.write_bytes(b'audio1')
    assert embedded_audio(epub) == ('audio/a b.m4a',)
    assert audio_matches_single_embedded_track(epub, track)
    track.write_bytes(b'audio2')
    assert not audio_matches_single_embedded_track(epub, track)
