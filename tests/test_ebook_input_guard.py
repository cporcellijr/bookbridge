import json
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.db.database_service import DatabaseService
from src.db.models import Book, Job
from src.services.alignment_service import AlignmentService
from src.sync_manager import SyncManager
from src.utils import ebook_utils
from src.utils.ebook_utils import EbookParser
from src.utils.polisher import Polisher


_CONTAINER = (
    '<?xml version="1.0"?><container version="1.0" '
    'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
    '<rootfile full-path="OEBPS/content.opf" '
    'media-type="application/oebps-package+xml"/></rootfiles></container>'
)


def _write_epub(path: Path, text: str) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", _CONTAINER)
        archive.writestr(
            "OEBPS/content.opf",
            '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" '
            'version="2.0" unique-identifier="uid"><metadata '
            'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Test</dc:title>'
            '<dc:identifier id="uid">test</dc:identifier></metadata><manifest>'
            '<item id="chapter" href="chapter.xhtml" '
            'media-type="application/xhtml+xml"/></manifest><spine>'
            '<itemref idref="chapter"/></spine></package>',
        )
        archive.writestr(
            "OEBPS/chapter.xhtml",
            f"<html><body><p>{text}</p></body></html>",
        )


@pytest.fixture
def worker(tmp_path):
    db = DatabaseService(str(tmp_path / "worker.db"))
    parser = EbookParser(tmp_path, tmp_path / "epub-cache")
    alignment = AlignmentService(db, Polisher())
    manager = SyncManager.__new__(SyncManager)
    manager.database_service = db
    manager.ebook_parser = parser
    manager.alignment_service = alignment
    manager.transcriber = MagicMock()
    manager.abs_client = MagicMock()
    manager.abs_client.get_item_details.return_value = {"media": {"chapters": []}}
    manager.booklore_client = MagicMock()
    manager.hardcover_client = MagicMock()
    manager.storyteller_client = MagicMock()
    manager.library_service = MagicMock()
    manager.migration_service = None
    manager.data_dir = tmp_path
    manager.books_dir = tmp_path
    manager.epub_cache_dir = tmp_path / "epub-cache"
    manager._dispatch_readalong_intent = MagicMock()
    adapter = MagicMock()
    manager._get_audio_source_name = MagicMock(return_value="ABS")
    manager._get_audio_source_adapter = MagicMock(return_value=adapter)
    manager.library_service.acquire_ebook.side_effect = lambda _details, book: str(tmp_path / book.ebook_filename)

    def save_book(filename: str, abs_id: str = "guarded-book", sync_mode: str = "audiobook") -> Book:
        book = Book(
            abs_id=abs_id,
            abs_title="Guarded Book",
            ebook_filename=filename,
            status="processing",
            sync_mode=sync_mode,
        )
        db.save_book(book)
        db.save_job(Job(abs_id=abs_id, retry_count=0, progress=0.25))
        return db.get_book(abs_id)

    try:
        yield manager, db, parser, adapter, save_book
    finally:
        db.db_manager.close()


@pytest.mark.parametrize(
    ("filename", "content", "error_fragment", "parser_called"),
    [
        ("companion.pdf", b"%PDF-1.7\n" + b"resources\n" * 100, "not an EPUB", False),
        ("broken.epub", b"not a zip", "no readable EPUB text", True),
    ],
)
def test_bad_ebook_fails_before_hash_audio_transcription_or_map(
    worker, tmp_path, filename, content, error_fragment, parser_called, caplog
):
    manager, db, parser, adapter, save_book = worker
    path = tmp_path / filename
    path.write_bytes(content)
    book = save_book(filename)
    original_filename = book.ebook_filename
    cache = tmp_path / "audio_cache" / book.abs_id
    cache.mkdir(parents=True)
    progress = cache / "_progress.json"
    progress.write_text(json.dumps({
        "done": True,
        "chunks_completed": 1,
        "transcript": [{
            "start": 0,
            "end": 1,
            "text": "completed words",
            "words": [
                {"word": "completed", "start": 0, "end": 0.5},
                {"word": "words", "start": 0.5, "end": 1},
            ],
        }],
    }), encoding="utf-8")
    completed_transcript = progress.read_bytes()
    (cache / "part.wav").write_bytes(b"audio")
    tracks = cache / "source_tracks"
    tracks.mkdir()
    (tracks / "track.m4b").write_bytes(b"audio")
    hash_spy = MagicMock(wraps=parser.get_kosync_id)
    extract_spy = MagicMock(wraps=parser.extract_text_and_map)

    with patch.object(parser, "get_kosync_id", hash_spy), patch.object(
        parser, "extract_text_and_map", extract_spy
    ):
        manager._run_background_job(book)

    saved_book = db.get_book(book.abs_id)
    job = db.get_latest_job(book.abs_id)
    assert saved_book.status == "failed_permanent"
    assert saved_book.ebook_filename == original_filename
    assert saved_book.kosync_doc_id is None
    assert job.retry_count == 1
    assert error_fragment in job.last_error
    hash_spy.assert_not_called()
    assert extract_spy.call_count == int(parser_called)
    assert progress.read_bytes() == completed_transcript
    assert not (cache / "part.wav").exists()
    assert not tracks.exists()
    if filename == "broken.epub":
        assert "Bad Zip file" in caplog.text
    assert manager.transcriber.transcribe_from_smil.call_count == 0
    assert manager.transcriber.process_audio.call_count == 0
    adapter.get_chapters.assert_not_called()
    adapter.get_audio_files.assert_not_called()
    assert manager.alignment_service._get_alignment(book.abs_id) is None


def test_valid_epub_is_parsed_from_cache_and_hash_locked(worker, tmp_path):
    manager, db, parser, _adapter, save_book = worker
    path = tmp_path / "valid.epub"
    _write_epub(path, "A real chapter with readable text.")
    book = save_book(path.name, sync_mode="ebook_only")
    manager.library_service.acquire_ebook.return_value = str(path)

    with patch("src.utils.ebook_utils.epub.read_epub", wraps=ebook_utils.epub.read_epub) as read_epub:
        assert parser.extract_text_and_map(path)[0]
        manager._run_background_job(book, library_service=manager.library_service)

    saved_book = db.get_book(book.abs_id)
    assert saved_book.status == "active"
    assert saved_book.kosync_doc_id == parser.get_kosync_id(path)
    assert read_epub.call_count == 1
    assert manager.transcriber.transcribe_from_smil.call_count == 0
    assert manager.transcriber.process_audio.call_count == 0


def test_valid_audiobook_epub_reaches_alignment(worker, tmp_path, monkeypatch):
    manager, db, parser, adapter, save_book = worker
    monkeypatch.setenv("CTC_ENABLED", "false")
    path = tmp_path / "narrated.epub"
    text = "A real chapter with readable text."
    _write_epub(path, text)
    book = save_book(path.name)
    book.duration = 10
    db.update_book_if_exists(book)
    manager.transcriber.transcribe_from_smil.return_value = None
    manager.transcriber.process_audio.return_value = [
        {"start": 0.0, "end": 10.0, "text": text}
    ]
    adapter.get_chapters.return_value = []
    adapter.get_audio_files.return_value = []

    manager._run_background_job(book, library_service=manager.library_service)

    assert db.get_book(book.abs_id).status == "active"
    assert book.kosync_doc_id == parser.get_kosync_id(path)
    assert manager.transcriber.process_audio.call_count == 1
    assert manager.alignment_service._get_alignment(book.abs_id)


def test_empty_epub_is_rejected_but_cbz_ebook_only_still_activates(worker, tmp_path):
    manager, db, parser, _adapter, save_book = worker
    empty_path = tmp_path / "empty.epub"
    _write_epub(empty_path, "   ")
    empty_book = save_book(empty_path.name, abs_id="empty-book", sync_mode="ebook_only")
    manager.library_service.acquire_ebook.return_value = str(empty_path)

    manager._run_background_job(empty_book, library_service=manager.library_service)

    assert db.get_book(empty_book.abs_id).status == "failed_permanent"
    assert "no readable EPUB text" in db.get_latest_job(empty_book.abs_id).last_error

    cbz_path = tmp_path / "pages.cbz"
    cbz_path.write_bytes(b"not an epub parser input")
    cbz_book = save_book(cbz_path.name, abs_id="cbz-book", sync_mode="ebook_only")
    manager.library_service.acquire_ebook.return_value = str(cbz_path)
    parser_call = MagicMock(wraps=parser.extract_text_and_map)
    with patch.object(parser, "extract_text_and_map", parser_call):
        manager._run_background_job(cbz_book, library_service=manager.library_service)

    assert db.get_book(cbz_book.abs_id).status == "active"
    parser_call.assert_not_called()
