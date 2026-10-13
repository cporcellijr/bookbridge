"""End-to-end recovery regressions from scheduler discovery through safe rebuild."""

import json
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.db.database_service import DatabaseService
from src.db.models import (
    Book, BookAlignment, BookAlignmentBackup, Job, KosyncDocument,
    ReadingSession, State, UserBook,
)
from src.services.alignment_service import AlignmentService
from src.sync_manager import SyncManager
from src.utils.ebook_utils import EbookParser
from src.utils.polisher import Polisher
from src.utils.transcriber import AudioTranscriber


_CONTAINER = (
    '<?xml version="1.0"?><container version="1.0" '
    'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
    '<rootfile full-path="OEBPS/content.opf" '
    'media-type="application/oebps-package+xml"/></rootfiles></container>'
)
_TEXT = " ".join(
    "recovery keeps the reader's original words and timing available for alignment"
    for _ in range(18)
)


def _write_epub(path: Path, text: str = _TEXT) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", _CONTAINER)
        archive.writestr(
            "OEBPS/content.opf",
            '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" '
            'version="2.0" unique-identifier="uid"><metadata '
            'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Recovery</dc:title>'
            '<dc:identifier id="uid">recovery</dc:identifier></metadata><manifest>'
            '<item id="chapter" href="chapter.xhtml" '
            'media-type="application/xhtml+xml"/></manifest><spine>'
            '<itemref idref="chapter"/></spine></package>',
        )
        archive.writestr("OEBPS/chapter.xhtml", f"<html><body><p>{text}</p></body></html>")


def _timed_segments(text: str = _TEXT):
    words = text.split()
    timed_words = [
        {"word": word, "start": index * 10.0 / len(words),
         "end": (index + 1) * 10.0 / len(words)}
        for index, word in enumerate(words)
    ]
    return [{"start": 0.0, "end": 10.0, "text": text, "words": timed_words}]


@pytest.fixture
def recovery(tmp_path):
    db = DatabaseService(str(tmp_path / "recovery.db"))
    data_dir = tmp_path / "data"
    cache_dir = data_dir / "epub_cache"
    cache_dir.mkdir(parents=True)
    parser = EbookParser(data_dir, cache_dir)
    polisher = Polisher()
    transcriber = AudioTranscriber(data_dir, MagicMock(), polisher)
    alignment = AlignmentService(db, polisher)

    manager = SyncManager.__new__(SyncManager)
    manager.database_service = db
    manager.ebook_parser = parser
    manager.alignment_service = alignment
    manager.transcriber = transcriber
    manager.epub_cache_dir = cache_dir
    manager.data_dir = data_dir
    manager.books_dir = data_dir
    manager._job_thread = None
    manager.library_service = MagicMock()
    manager.abs_client = MagicMock()
    manager._get_audio_source_adapter = MagicMock(return_value=MagicMock())
    manager._get_audio_source_name = MagicMock(return_value="ABS")
    manager._dispatch_readalong_intent = MagicMock()
    try:
        yield manager, db, parser, transcriber, alignment, cache_dir
    finally:
        db.db_manager.close()


def _seed(recovery, *, pdf_bytes=b"%PDF-1.7\ncompanion", current_hash=None,
          map_json='[{"char":0,"ts":0},{"char":0,"ts":10}]', total_chars=0,
          include_timed_cache=True, abs_id="cwa-recovery-1516"):
    manager, db, parser, transcriber, _alignment, cache_dir = recovery
    epub_name = "reader-copy-1516.epub"
    epub_path = cache_dir / epub_name
    _write_epub(epub_path)
    original_hash = parser.get_kosync_id(epub_path)
    pdf_path = cache_dir / f"{abs_id}_direct.pdf"
    pdf_path.write_bytes(pdf_bytes)
    pdf_hash = parser.get_kosync_id(pdf_path)
    book = Book(
        abs_id=abs_id, abs_title="Recovery fixture", ebook_filename="companion.pdf",
        original_ebook_filename=epub_name, ebook_source="CWA", ebook_source_id="1516",
        kosync_doc_id=current_hash if current_hash is not None else pdf_hash,
        sync_mode="audiobook", status="active", duration=10.0,
    )
    db.save_book(book)
    db.save_job(Job(abs_id=abs_id, last_attempt=1.0, retry_count=2,
                    last_error="old alignment failure", progress=0.6))
    with db.get_session() as session:
        session.add(BookAlignment(
            abs_id=abs_id, alignment_map_json=map_json, align_method="linear",
            total_chars=total_chars,
        ))
    if include_timed_cache:
        transcript_path = transcriber.cache_root / abs_id / "_progress.json"
        transcript_path.parent.mkdir(parents=True, exist_ok=True)
        transcript_path.write_text(json.dumps({
            "done": True, "chunks_completed": 1, "transcript": _timed_segments(),
        }), encoding="utf-8")
    return book, epub_path, pdf_path, original_hash, pdf_hash


class _InlineThread:
    """Capture the exact scheduler-launched worker and execute it synchronously."""

    def __init__(self, *, target, args, daemon):
        self.target, self.args, self.daemon = target, args, daemon

    def start(self):
        self.target(*self.args)

    def is_alive(self):
        return False


def test_scheduler_proves_pdf_hash_then_rebuilds_from_cached_inputs_only(recovery):
    manager, db, parser, transcriber, alignment, _cache_dir = recovery
    book, epub_path, _pdf_path, original_hash, pdf_hash = _seed(recovery)
    reader = db.create_user("recovery-reader")
    db.ensure_linked_kosync_document(pdf_hash, book.abs_id)
    with db.get_session() as session:
        session.add_all([
            UserBook(user_id=reader.id, abs_id=book.abs_id),
            State(abs_id=book.abs_id, client_name="ABS", percentage=0.61, user_id=reader.id),
            ReadingSession(abs_id=book.abs_id, session_type="AUDIOBOOK", start_time=1,
                           end_time=9, duration_seconds=8, start_progress=0.4,
                           end_progress=0.61, leader_client="ABS", user_id=reader.id),
            BookAlignmentBackup(abs_id=book.abs_id,
                                alignment_map_json='[{"char":3,"ts":1},{"char":8,"ts":2}]',
                                align_method="lexical", total_chars=100),
        ])
    prior_backup = '[{"char":3,"ts":1},{"char":8,"ts":2}]'
    transcript_file = transcriber.cache_root / book.abs_id / "_progress.json"
    timed_cache = transcript_file.read_bytes()
    details_spy = MagicMock(wraps=manager.abs_client.get_item_details)
    inference_spy = MagicMock(wraps=transcriber.process_audio)
    smil_spy = MagicMock(wraps=transcriber.transcribe_from_smil)
    transcriber.process_audio = inference_spy
    transcriber.transcribe_from_smil = smil_spy
    manager.abs_client.get_item_details = details_spy

    with patch("src.sync_manager.threading.Thread", _InlineThread):
        manager.check_pending_jobs()

    saved = db.get_book(book.abs_id)
    job = db.get_latest_job(book.abs_id, kind="alignment")
    assert saved.status == "active"
    assert saved.ebook_filename == epub_path.name
    assert saved.kosync_doc_id == original_hash
    assert job.stage is None and job.retry_count == 0 and job.progress == 1.0
    assert details_spy.call_count == 0
    manager.library_service.acquire_ebook.assert_not_called()
    assert inference_spy.call_count == 0
    assert smil_spy.call_count == 0
    assert transcript_file.read_bytes() == timed_cache
    assert alignment._get_alignment(book.abs_id)
    assert max(point.get("char", point.get("global_char", 0))
               for point in alignment._get_alignment(book.abs_id)) > 0
    with db.get_session() as session:
        assert session.query(BookAlignmentBackup).filter_by(abs_id=book.abs_id).one().alignment_map_json == prior_backup
        assert session.query(State).filter_by(abs_id=book.abs_id, user_id=reader.id).one().percentage == 0.61
        assert session.query(ReadingSession).filter_by(abs_id=book.abs_id, user_id=reader.id).count() == 1
        assert session.query(UserBook).filter_by(abs_id=book.abs_id, user_id=reader.id).count() == 1
        assert session.query(KosyncDocument).filter_by(document_hash=pdf_hash).one().linked_abs_id is None


@pytest.mark.parametrize("bad_evidence", ["missing_pdf", "bad_pdf_magic", "wrong_pdf_hash", "missing_transcript"])
def test_scheduler_marks_pdf_candidate_manual_without_changing_mapping(recovery, bad_evidence):
    manager, db, _parser, _transcriber, _alignment, cache_dir = recovery
    if bad_evidence == "missing_pdf":
        book, _epub, pdf_path, _original_hash, _pdf_hash = _seed(recovery)
        pdf_path.unlink()
    elif bad_evidence == "bad_pdf_magic":
        book, _epub, _pdf, _original_hash, _pdf_hash = _seed(
            recovery, pdf_bytes=b"not a pdf\nbody",
        )
    elif bad_evidence == "wrong_pdf_hash":
        book, _epub, _pdf, _original_hash, _pdf_hash = _seed(
            recovery, current_hash="not-the-companion-pdf-hash",
        )
    else:
        book, _epub, _pdf, _original_hash, _pdf_hash = _seed(
            recovery, include_timed_cache=False,
        )
    prior_filename, prior_hash = book.ebook_filename, book.kosync_doc_id

    manager._recover_ebook_input_candidates()

    current = db.get_book(book.abs_id)
    job = db.get_latest_job(book.abs_id, kind="alignment")
    assert current.ebook_filename == prior_filename
    assert current.kosync_doc_id == prior_hash
    assert current.status == "failed_permanent"
    assert job.stage == "ebook_input_recovery_manual"
    assert job.retry_count == 2
    job_id, failure_note = job.id, job.last_error
    manager._recover_ebook_input_candidates()
    repeated = db.get_latest_job(book.abs_id, kind="alignment")
    assert repeated.id == job_id
    assert repeated.stage == "ebook_input_recovery_manual"
    assert repeated.retry_count == 2 and repeated.last_error == failure_note


def test_zero_char_metadata_without_actual_zero_anchors_is_not_repaired(recovery):
    manager, db, _parser, _transcriber, _alignment, _cache_dir = recovery
    book, _epub, _pdf, _original_hash, _pdf_hash = _seed(
        recovery, map_json='[{"char":1,"ts":0},{"char":40,"ts":10}]',
        total_chars=0, current_hash="ambiguous-legacy-hash",
    )

    manager._recover_ebook_input_candidates()

    saved = db.get_book(book.abs_id)
    assert saved.status == "failed_permanent"
    assert saved.ebook_filename == "companion.pdf"
    assert saved.kosync_doc_id == "ambiguous-legacy-hash"
    assert db.get_latest_job(book.abs_id, kind="alignment").stage == "ebook_input_recovery_manual"


@pytest.mark.parametrize("cwa_id", [None, "legacy-cwa-label"])
def test_missing_or_nonnumeric_cwa_id_is_manual_only(recovery, cwa_id):
    manager, db, _parser, _transcriber, _alignment, _cache_dir = recovery
    book, _epub, _pdf, _original_hash, pdf_hash = _seed(recovery)
    book.ebook_source_id = cwa_id
    db.save_book(book)

    manager._recover_ebook_input_candidates()

    saved = db.get_book(book.abs_id)
    assert saved.status == "failed_permanent"
    assert saved.ebook_filename == "companion.pdf"
    assert saved.kosync_doc_id == pdf_hash
    assert db.get_latest_job(book.abs_id, kind="alignment").stage == "ebook_input_recovery_manual"


def test_missing_original_cache_is_manual_and_does_not_rewrite_identity(recovery):
    manager, db, _parser, _transcriber, _alignment, _cache_dir = recovery
    book, epub_path, _pdf, _original_hash, pdf_hash = _seed(recovery)
    epub_path.unlink()

    manager._recover_ebook_input_candidates()

    saved = db.get_book(book.abs_id)
    assert saved.status == "failed_permanent"
    assert saved.original_ebook_filename == "reader-copy-1516.epub"
    assert saved.ebook_filename == "companion.pdf"
    assert saved.kosync_doc_id == pdf_hash
    assert db.get_latest_job(book.abs_id, kind="alignment").stage == "ebook_input_recovery_manual"


def test_actual_zero_anchor_map_with_original_hash_is_proof_and_queues_rebuild(recovery):
    manager, db, _parser, _transcriber, _alignment, _cache_dir = recovery
    book, _epub, _pdf, original_hash, _pdf_hash = _seed(recovery)
    book.ebook_filename = book.original_ebook_filename
    book.kosync_doc_id = original_hash
    db.save_book(book)

    manager._recover_ebook_input_candidates()

    queued = db.get_book(book.abs_id)
    job = db.get_latest_job(book.abs_id, kind="alignment")
    assert queued.status == "pending"
    assert queued.ebook_filename == book.original_ebook_filename
    assert queued.kosync_doc_id == original_hash
    assert job.stage == "ebook_input_recovery"


def test_explicit_pending_from_manual_flag_clears_recovery_stage_for_normal_retry(recovery):
    manager, db, _parser, _transcriber, _alignment, _cache_dir = recovery
    book, _epub, _pdf, _original_hash, _pdf_hash = _seed(
        recovery, current_hash="ambiguous-not-pdf-hash",
    )
    manager._recover_ebook_input_candidates()
    flagged = db.get_book(book.abs_id)
    assert flagged.status == "failed_permanent"
    assert db.get_latest_job(book.abs_id, kind="alignment").stage == "ebook_input_recovery_manual"

    flagged.status = "pending"
    db.save_book(flagged)
    manager._run_background_job = MagicMock()
    with patch("src.sync_manager.threading.Thread") as thread_cls:
        manager.check_pending_jobs()
        target = thread_cls.call_args.kwargs["target"]
        args = thread_cls.call_args.kwargs["args"]
        assert target is manager._run_background_job
        assert args[0].status == "processing"
        target(*args)

    queued = db.get_latest_job(book.abs_id, kind="alignment")
    assert queued.stage is None
    assert queued.retry_count == 0
    manager._run_background_job.assert_called_once()


def test_restart_keeps_recovery_stage_and_retained_cache_retryable(recovery):
    manager, db, _parser, transcriber, _alignment, _cache_dir = recovery
    book, epub_path, _pdf, _hash, _pdf_hash = _seed(recovery)
    manager._recover_ebook_input_candidates()
    recovery_identity = db.get_latest_job(book.abs_id, kind="alignment").recovery_identity
    queued = db.get_book(book.abs_id)
    queued.status = "processing"
    db.save_book(queued)
    manager._promote_alignment_backed_book = MagicMock(return_value=False)

    manager.cleanup_stale_jobs()

    interrupted = db.get_book(book.abs_id)
    job = db.get_latest_job(book.abs_id, kind="alignment")
    assert interrupted.status == "failed_retry_later"
    assert job.stage == "ebook_input_recovery"
    assert job.recovery_identity == recovery_identity
    assert job.retry_count == 1
    assert epub_path.is_file()
    assert transcriber.get_cached_word_timed_transcript(book.abs_id, 10.0)


def test_repeated_restart_at_retry_limit_manualizes_recovery_without_deleting_cache(recovery, monkeypatch):
    manager, db, _parser, transcriber, _alignment, _cache_dir = recovery
    book, epub_path, _pdf, _hash, _pdf_hash = _seed(recovery)
    manager._recover_ebook_input_candidates()
    queued = db.get_book(book.abs_id)
    queued.status = "processing"
    db.save_book(queued)
    manager._promote_alignment_backed_book = MagicMock(return_value=False)
    monkeypatch.setenv("JOB_MAX_RETRIES", "1")

    manager.cleanup_stale_jobs()

    flagged = db.get_book(book.abs_id)
    job = db.get_latest_job(book.abs_id, kind="alignment")
    assert flagged.status == "failed_permanent"
    assert job.stage == "ebook_input_recovery_manual"
    assert job.retry_count == 1
    assert epub_path.is_file()
    assert transcriber.get_cached_word_timed_transcript(book.abs_id, 10.0)


def test_healthy_mapping_takes_scheduler_fast_path_without_parsing_or_library_scan(recovery):
    manager, db, parser, _transcriber, _alignment, cache_dir = recovery
    name = "healthy-original.epub"
    path = cache_dir / name
    _write_epub(path)
    book = Book(
        abs_id="healthy-cwa-1", abs_title="Healthy", ebook_filename=name,
        original_ebook_filename=name, ebook_source="CWA", ebook_source_id="1",
        kosync_doc_id=parser.get_kosync_id(path), sync_mode="audiobook", status="active",
        duration=10,
    )
    db.save_book(book)
    manager.library_service.sync_library_books.reset_mock()
    with patch.object(parser, "extract_text_and_map") as parse:
        manager.check_pending_jobs()
    parse.assert_not_called()
    manager.library_service.sync_library_books.assert_not_called()
    assert db.get_book(book.abs_id).status == "active"


def test_sync_cycle_blocks_suspicious_candidate_before_any_progress_fetch_or_write(recovery):
    manager, db, _parser, _transcriber, _alignment, _cache_dir = recovery
    book, _epub, _pdf, _original_hash, _pdf_hash = _seed(recovery)
    book.status = "active"
    db.save_book(book)
    manager.sync_clients = {"ABS": MagicMock()}
    manager._sync_cycle_ebook_cache = {}
    manager._sync_cycle_local_epub_cache = {}
    manager._storyteller_epub_ensure_attempted = set()
    manager._last_library_sync = float("inf")
    manager._flush_reading_sessions = MagicMock()
    manager._filter_books_for_current_user = lambda books, _bulk: books
    manager._filter_books_by_access = lambda books, _bulk: books
    manager._fetch_states_parallel = MagicMock()
    manager.sync_clients["ABS"].update_progress = MagicMock()

    with ThreadPoolExecutor(max_workers=1) as pool:
        manager._run_sync_cycle(book.abs_id, pool)

    manager._fetch_states_parallel.assert_not_called()
    manager.sync_clients["ABS"].update_progress.assert_not_called()


def _set_recovery_map_sentinel(db, alignment, abs_id):
    alignment._save_alignment(
        abs_id, [{"char": 0, "ts": 0}, {"char": 999, "ts": 10}],
        "new-map-sentinel", total_chars=999,
    )
    with db.get_session() as session:
        session.merge(BookAlignmentBackup(
            abs_id=abs_id,
            alignment_map_json='[{"char":0,"ts":1},{"char":333,"ts":9}]',
            align_method="backup-sentinel",
            total_chars=333,
        ))


@pytest.mark.parametrize(
    "race",
    ["ebook_remap", "audio_remap", "newer_map", "deleted", "job_changed", "hash_relinked"],
)
def test_scheduler_recovery_does_not_publish_or_activate_stale_generation(recovery, race):
    manager, db, _parser, _transcriber, alignment, _cache_dir = recovery
    book, _epub_path, _pdf_path, _original_hash, _pdf_hash = _seed(recovery)
    manager._recover_ebook_input_candidates()
    queued = db.get_book(book.abs_id)
    queued_job = db.get_latest_job(book.abs_id, kind="alignment")
    queued_identity = queued_job.recovery_identity
    assert len(queued_identity) == 64
    assert queued_identity == db.ebook_input_recovery_identity(queued)

    entered_generation = threading.Event()
    resume_generation = threading.Event()
    original_generate = alignment._generate_alignment_map_with_method

    def gated_generate(*args, **kwargs):
        result = original_generate(*args, **kwargs)
        entered_generation.set()
        assert resume_generation.wait(30), "race controller did not resume the recovery worker"
        return result

    with patch.object(alignment, "_generate_alignment_map_with_method", gated_generate):
        manager.check_pending_jobs()
        assert entered_generation.wait(30), "scheduler did not reach real alignment generation"
        current = db.get_book(book.abs_id)
        if race == "deleted":
            db.delete_book(book.abs_id)
        elif race == "ebook_remap":
            current.ebook_source_id = "9999"
            current.ebook_filename = "new-pairing.epub"
            current.original_ebook_filename = "new-pairing.epub"
            current.kosync_doc_id = "new-pairing-hash"
            current.status = "pending"
            db.save_book(current)
            _set_recovery_map_sentinel(db, alignment, book.abs_id)
        elif race == "audio_remap":
            current.audio_source = "BookOrbit"
            current.audio_source_id = "new-audio"
            current.status = "pending"
            db.save_book(current)
            _set_recovery_map_sentinel(db, alignment, book.abs_id)
        elif race == "newer_map":
            _set_recovery_map_sentinel(db, alignment, book.abs_id)
        elif race == "job_changed":
            latest_job_id = db.get_latest_job(book.abs_id, kind="alignment").id
            with db.get_session() as session:
                latest = session.get(Job, latest_job_id)
                latest.stage = "ebook_input_recovery_manual"
                latest.last_error = "new manual review"
            _set_recovery_map_sentinel(db, alignment, book.abs_id)
        elif race == "hash_relinked":
            other = Book(abs_id="other-pairing", ebook_filename="other.epub", status="pending")
            db.save_book(other)
            db.ensure_linked_kosync_document(current.kosync_doc_id, other.abs_id)
            _set_recovery_map_sentinel(db, alignment, book.abs_id)

        resume_generation.set()
        manager._job_thread.join(30)
        assert not manager._job_thread.is_alive()

    if race == "deleted":
        assert db.get_book(book.abs_id) is None
        with db.get_session() as session:
            assert session.query(BookAlignment).filter_by(abs_id=book.abs_id).count() == 0
        return

    saved = db.get_book(book.abs_id)
    job = db.get_latest_job(book.abs_id, kind="alignment")
    assert saved.status != "active"
    assert alignment._get_alignment(book.abs_id) == [
        {"char": 0, "ts": 0}, {"char": 999, "ts": 10},
    ]
    assert db.get_alignment_method(book.abs_id) == "new-map-sentinel"
    with db.get_session() as session:
        backup = session.query(BookAlignmentBackup).filter_by(abs_id=book.abs_id).one()
        assert backup.align_method == "backup-sentinel"
        assert backup.total_chars == 333
    if race == "job_changed":
        assert job.stage == "ebook_input_recovery_manual"
        assert job.last_error == "new manual review"
    if race == "hash_relinked":
        assert db.get_kosync_document(saved.kosync_doc_id).linked_abs_id == "other-pairing"


def test_same_duration_audio_remap_with_retained_stage_is_manual_on_next_scheduler_check(recovery):
    manager, db, _parser, transcriber, alignment, _cache_dir = recovery
    book, _epub_path, _pdf_path, _original_hash, _pdf_hash = _seed(recovery)
    manager._recover_ebook_input_candidates()
    old_identity = db.get_latest_job(book.abs_id, kind="alignment").recovery_identity
    entered_generation = threading.Event()
    resume_generation = threading.Event()
    original_generate = alignment._generate_alignment_map_with_method

    def gated_generate(*args, **kwargs):
        result = original_generate(*args, **kwargs)
        entered_generation.set()
        assert resume_generation.wait(30), "race controller did not resume the recovery worker"
        return result

    with patch.object(alignment, "_generate_alignment_map_with_method", gated_generate):
        manager.check_pending_jobs()
        assert entered_generation.wait(30)
        remapped = db.get_book(book.abs_id)
        remapped.audio_source = "BookOrbit"
        remapped.audio_source_id = "same-duration-new-audio"
        remapped.status = "pending"
        db.save_book(remapped)
        resume_generation.set()
        manager._job_thread.join(30)
        assert not manager._job_thread.is_alive()

    retained = db.get_latest_job(book.abs_id, kind="alignment")
    assert retained.stage == "ebook_input_recovery"
    assert retained.recovery_identity == old_identity
    assert db.get_book(book.abs_id).duration == 10.0
    manager._job_thread = None  # next scheduler tick after worker/restart
    manager.check_pending_jobs()
    assert manager._job_thread is not None
    manager._job_thread.join(30)
    assert not manager._job_thread.is_alive()

    after = db.get_book(book.abs_id)
    manual = db.get_latest_job(book.abs_id, kind="alignment")
    assert after.status == "failed_permanent"
    assert manual.stage == "ebook_input_recovery_manual"
    assert manual.recovery_identity == old_identity
    assert after.audio_source_id == "same-duration-new-audio"
    assert transcriber.get_cached_word_timed_transcript(book.abs_id, 10.0)
    manager.library_service.acquire_ebook.assert_not_called()


def test_legacy_recovery_stage_without_identity_is_manual(recovery):
    manager, db, _parser, _transcriber, _alignment, _cache_dir = recovery
    book, _epub_path, _pdf_path, _original_hash, _pdf_hash = _seed(recovery)
    manager._recover_ebook_input_candidates()
    with db.get_session() as session:
        job = session.query(Job).filter_by(abs_id=book.abs_id, kind="alignment").one()
        job.recovery_identity = None

    manager.check_pending_jobs()
    assert manager._job_thread is not None
    manager._job_thread.join(30)
    assert not manager._job_thread.is_alive()

    saved = db.get_book(book.abs_id)
    job = db.get_latest_job(book.abs_id, kind="alignment")
    assert saved.status == "failed_permanent"
    assert job.stage == "ebook_input_recovery_manual"
    assert job.recovery_identity is None
    manager.library_service.acquire_ebook.assert_not_called()
