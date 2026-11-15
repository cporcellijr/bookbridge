"""Cover extraction must not load read-aloud audio or publish partial images."""

import os
import zipfile
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.utils.ebook_utils import EbookParser


def make_epub(path, manifest, metadata='', image_name='OPS/images/art.jpg', image=b'cover bytes'):
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('META-INF/container.xml', '''
            <container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
            <rootfiles><rootfile full-path="OPS/package.opf"/></rootfiles></container>''')
        archive.writestr('OPS/package.opf', f'''
            <package xmlns="http://www.idpf.org/2007/opf">
            <metadata>{metadata}</metadata><manifest>{manifest}
            <item id="audio" href="audio.mp3" media-type="audio/mpeg"/>
            <item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/>
            </manifest></package>''')
        archive.writestr(image_name, image)
        archive.writestr('OPS/audio.mp3', b'audio' * 10000)
        archive.writestr('OPS/chapter.xhtml', '<html><body>Text</body></html>')


@pytest.mark.parametrize(('manifest', 'metadata'), [
    ('<item id="art" href="images/art.jpg" media-type="image/jpeg" properties="nav cover-image"/>', ''),
    ('<item id="art" href="images/art.jpg" media-type="image/jpeg"/>', '<meta name="cover" content="art"/>'),
    ('<item id="art" href="images/cover.jpg" media-type="image/jpeg"/>', ''),
    ('<item id="art" href="images/my%20art.jpg#fragment" media-type="image/jpeg" properties="cover-image"/>', ''),
])
def test_reads_only_metadata_and_cover(tmp_path, monkeypatch, manifest, metadata):
    image_name = 'OPS/images/art.jpg'
    if 'cover.jpg' in manifest:
        image_name = 'OPS/images/cover.jpg'
    elif 'my%20art' in manifest:
        image_name = 'OPS/images/my art.jpg'
    source, target = tmp_path / 'book.epub', tmp_path / 'cover.jpg'
    make_epub(source, manifest, metadata, image_name)
    opened = []
    original_open = zipfile.ZipFile.open

    def guarded_open(archive, name, *args, **kwargs):
        assert name in ('META-INF/container.xml', 'OPS/package.opf', image_name)
        opened.append(name)
        return original_open(archive, name, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, 'open', guarded_open)
    monkeypatch.setattr('src.utils.ebook_utils._read_epub', Mock(side_effect=AssertionError('whole-book read')))
    assert EbookParser(str(tmp_path)).extract_cover(source, target)
    assert target.read_bytes() == b'cover bytes'
    assert opened == ['META-INF/container.xml', 'OPS/package.opf', image_name]


def test_declared_cover_precedes_filename_heuristic(tmp_path):
    source, target = tmp_path / 'book.epub', tmp_path / 'cover.jpg'
    make_epub(source, '''
        <item id="wrong" href="back-cover.jpg" media-type="image/jpeg"/>
        <item id="art" href="images/art.jpg" media-type="image/jpeg" properties="cover-image"/>''')
    with zipfile.ZipFile(source, 'a') as archive:
        archive.writestr('OPS/back-cover.jpg', b'wrong')
    assert EbookParser(str(tmp_path)).extract_cover(source, target)
    assert target.read_bytes() == b'cover bytes'


@pytest.mark.parametrize('failure', ['missing', 'malformed', 'oversized_metadata', 'oversized_image', 'no_cover'])
def test_failures_preserve_existing_cover(tmp_path, monkeypatch, failure):
    source, target = tmp_path / 'book.epub', tmp_path / 'cover.jpg'
    target.write_bytes(b'existing')
    manifest = '<item id="art" href="images/art.jpg" media-type="image/jpeg" properties="cover-image"/>'
    if failure == 'no_cover':
        manifest = ''
    if failure != 'missing':
        make_epub(source, manifest, image=b'x' * (33 * 1024 * 1024) if failure == 'oversized_image' else b'cover')
    if failure in ('malformed', 'oversized_metadata'):
        source.unlink()
        with zipfile.ZipFile(source, 'w') as archive:
            archive.writestr('META-INF/container.xml', b'<' if failure == 'malformed' else b'x' * (2 * 1024 * 1024 + 1))
    opened = []
    original_open = zipfile.ZipFile.open

    def guarded_open(archive, name, *args, **kwargs):
        opened.append(name)
        return original_open(archive, name, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, 'open', guarded_open)
    assert not EbookParser(str(tmp_path)).extract_cover(source, target)
    if failure == 'oversized_metadata':
        assert 'META-INF/container.xml' not in opened
    if failure == 'oversized_image':
        assert 'OPS/images/art.jpg' not in opened
    assert target.read_bytes() == b'existing'
    assert sorted(p.name for p in tmp_path.iterdir()) == (['cover.jpg'] if failure == 'missing' else ['book.epub', 'cover.jpg'])


def test_atomic_publication_failure_cleans_temporary_file(tmp_path, monkeypatch):
    source, target = tmp_path / 'book.epub', tmp_path / 'cover.jpg'
    make_epub(source, '<item id="art" href="images/art.jpg" media-type="image/jpeg" properties="cover-image"/>')
    target.write_bytes(b'existing')

    def fail_replace(temporary, destination):
        assert target.read_bytes() == b'existing'
        assert os.path.dirname(temporary) == str(tmp_path)
        raise OSError('simulated replace failure')

    monkeypatch.setattr(os, 'replace', fail_replace)
    assert not EbookParser(str(tmp_path)).extract_cover(source, target)
    assert target.read_bytes() == b'existing'
    assert sorted(p.name for p in tmp_path.iterdir()) == ['book.epub', 'cover.jpg']


def test_concurrent_requests_extract_once(tmp_path, monkeypatch):
    import src.web_server as web

    source, target = tmp_path / 'book.epub', tmp_path / 'abc.jpg'
    make_epub(source, '<item id="art" href="images/art.jpg" media-type="image/jpeg" properties="cover-image"/>')
    parser = EbookParser(str(tmp_path))
    entered, release, second_started = Event(), Event(), Event()
    original_extract = parser.extract_cover

    def slow_extract(*args):
        entered.set()
        assert release.wait(10)
        return original_extract(*args)

    parser.extract_cover = Mock(side_effect=slow_extract)
    parser.resolve_book_path = Mock(return_value=source)
    monkeypatch.setattr(web, 'COVERS_DIR', tmp_path, raising=False)
    monkeypatch.setattr(web, 'database_service', SimpleNamespace(
        get_book_by_kosync_id=lambda _: SimpleNamespace(ebook_filename='book.epub')), raising=False)
    monkeypatch.setattr(web, 'container', SimpleNamespace(ebook_parser=lambda: parser), raising=False)
    monkeypatch.setattr(web, '_cover_fresh_until', {})
    monkeypatch.setattr(web, '_schedule_cover_recheck', lambda _: None)
    monkeypatch.setattr(web, '_send_cover_cached', lambda _: target.read_bytes())

    def second_request():
        second_started.set()
        return web.serve_cover('abc.jpg')

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(web.serve_cover, 'abc.jpg')
        assert entered.wait(10)
        second = pool.submit(second_request)
        assert second_started.wait(10)
        release.set()
        assert first.result(timeout=10) == second.result(timeout=10) == b'cover bytes'
    assert parser.extract_cover.call_count == 1
