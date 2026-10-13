import json

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from src.db.database_service import DatabaseService
from src.db.models import Book, BookAlignment, BookAlignmentBackup, Job, KosyncDocument


@pytest.fixture
def db(tmp_path):
    service = DatabaseService(str(tmp_path / "recovery-atomic.db"))
    try:
        yield service
    finally:
        service.db_manager.close()


def _book(db, **overrides):
    fields = {
        "abs_id": "book",
        "audio_source": "ABS",
        "audio_source_id": "audio-1",
        "audio_duration": 100.0,
        "duration": 100.0,
        "ebook_source": "CWA",
        "ebook_source_id": "ebook-1",
        "ebook_filename": "repaired.epub",
        "original_ebook_filename": "original.pdf",
        "kosync_doc_id": "good-hash",
        "sync_mode": "audiobook",
        "status": "pending",
    }
    fields.update(overrides)
    return db.save_book(Book(**fields))


def _stage(db, book):
    job = Job(
        abs_id=book.abs_id,
        last_attempt=100.0,
        retry_count=2,
        last_error="retrying",
        progress=0.4,
        kind="alignment",
        stage="ebook_input_recovery",
        recovery_identity=db.ebook_input_recovery_identity(book),
    )
    db.save_job(job)
    return db.get_latest_job(book.abs_id, kind="alignment")


def _old_map(db, *, backup=None):
    with db.get_session() as session:
        session.add(BookAlignment(
            abs_id="book",
            alignment_map_json='[{"char":0,"ts":0},{"char":10,"ts":5}]',
            align_method="lexical",
            total_chars=10,
            quality_score=0.4,
            quality_detail="old quality",
            segments_json='[{"old":true}]',
        ))
        if backup:
            session.add(BookAlignmentBackup(
                abs_id="book", alignment_map_json=backup,
                align_method="saved", total_chars=20, segments_json="saved segments",
            ))
    return db.get_ebook_input_recovery_alignment("book")[1]


def _complete(db, book, job, map_timestamp, **overrides):
    options = {
        "expected_job": job,
        "alignment_map": [{"char": 0, "ts": 0.0}, {"char": 20, "ts": 12.0}],
        "alignment_method": "lexical_timed",
        "total_chars": 20,
        "segments_json": '[{"start":0,"end":20}]',
        "quality_score": 0.9,
        "quality_detail": "timed",
    }
    options.update(overrides)
    return db.complete_ebook_input_recovery(
        book.abs_id, book, map_timestamp, **options
    )


def _map(db):
    with db.get_session() as session:
        row = session.query(BookAlignment).filter_by(abs_id="book").one()
        return row.alignment_map_json, row.align_method, row.total_chars, row.segments_json


def test_recovery_identity_covers_pairing_fields_but_not_status(db):
    book = _book(db)
    original = db.ebook_input_recovery_identity(book)
    book.status = "active"
    assert db.ebook_input_recovery_identity(book) == original

    for field, value in [
        ("abs_id", "other"), ("audio_source", "BookOrbit"),
        ("audio_source_id", "audio-2"), ("audio_duration", 101.0),
        ("duration", 101.0), ("ebook_source", "Storyteller"),
        ("ebook_source_id", "ebook-2"), ("ebook_filename", "other.epub"),
        ("original_ebook_filename", "other.pdf"),
        ("kosync_doc_id", "other-hash"), ("sync_mode", "ebook_only"),
    ]:
        changed = Book(**{key: getattr(book, key) for key in (
            "abs_id", "audio_source", "audio_source_id", "audio_duration", "duration",
            "ebook_source", "ebook_source_id", "ebook_filename",
            "original_ebook_filename", "kosync_doc_id", "sync_mode",
        )})
        setattr(changed, field, value)
        assert db.ebook_input_recovery_identity(changed) != original, field


def test_completion_atomically_saves_map_book_job_and_first_backup(db):
    book = _book(db)
    job = _stage(db, book)
    old_timestamp = _old_map(db)

    assert _complete(db, book, job, old_timestamp)

    saved = db.get_book(book.abs_id)
    completed_job = db.get_latest_job(book.abs_id, kind="alignment")
    assert saved.status == "active" and saved.transcript_file == "DB_MANAGED"
    assert completed_job.stage is None
    assert (completed_job.retry_count, completed_job.progress, completed_job.last_error) == (0, 1.0, None)
    assert completed_job.recovery_identity is None
    raw_map, method, total_chars, segments = _map(db)
    assert json.loads(raw_map) == [{"char": 0, "ts": 0.0}, {"char": 20, "ts": 12.0}]
    assert (method, total_chars, segments) == (
        "lexical_timed", 20, '[{"start":0,"end":20}]'
    )
    with db.get_session() as session:
        backup = session.query(BookAlignmentBackup).filter_by(abs_id="book").one()
        assert json.loads(backup.alignment_map_json) == [
            {"char": 0, "ts": 0}, {"char": 10, "ts": 5}
        ]
        assert backup.align_method == "lexical" and backup.total_chars == 10
        assert backup.segments_json == '[{"old":true}]'


def test_completion_preserves_existing_backup(db):
    book = _book(db)
    job = _stage(db, book)
    old_timestamp = _old_map(db, backup='[{"char":0,"ts":0},{"char":20,"ts":9}]')

    assert _complete(db, book, job, old_timestamp)

    with db.get_session() as session:
        backup = session.query(BookAlignmentBackup).filter_by(abs_id="book").one()
        assert backup.alignment_map_json == '[{"char":0,"ts":0},{"char":20,"ts":9}]'
        assert backup.align_method == "saved" and backup.total_chars == 20


def test_completion_accepts_segmented_map_with_timestamp_reset_and_padding(db):
    book = _book(db)
    job = _stage(db, book)
    old_timestamp = _old_map(db)
    segmented = [
        {"char": 0, "ts": 0.0},
        {"char": 10, "ts": 8.0},
        {"char": 10, "ts": 1.0},
        {"char": 20, "ts": 2.0},
    ]

    assert _complete(
        db, book, job, old_timestamp,
        alignment_map=segmented,
        segments_json='[{"char_start":0,"char_end":10,"ts_start":0,"ts_end":8},'
                      '{"char_start":10,"char_end":20,"ts_start":1,"ts_end":2}]',
    )


def test_completion_rejects_legacy_job_without_recovery_identity(db):
    book = _book(db)
    job = _stage(db, book)
    db.update_job_by_id(job.id, recovery_identity=None)
    legacy_job = db.get_latest_job(book.abs_id, kind="alignment")
    old_timestamp = _old_map(db)
    original_map = _map(db)

    assert not _complete(db, book, legacy_job, old_timestamp)
    assert _map(db) == original_map
    assert db.get_book(book.abs_id).status == "pending"


def test_completion_rejects_snapshot_with_mismatched_durable_identity(db):
    book = _book(db)
    job = _stage(db, book)
    old_timestamp = _old_map(db)
    book.ebook_source_id = "different-ebook"

    assert not _complete(db, book, job, old_timestamp)
    assert db.get_book("book").status == "pending"


def test_failed_map_flush_rolls_back_book_job_map_and_backup(db):
    book = _book(db)
    job = _stage(db, book)
    old_timestamp = _old_map(db)
    original_map = _map(db)
    original_job = db.get_latest_job(book.abs_id, kind="alignment")

    def fail_flush(_session, _flush_context, _instances):
        raise RuntimeError("forced map flush failure")

    event.listen(Session, "before_flush", fail_flush)
    try:
        with pytest.raises(RuntimeError, match="forced map flush failure"):
            _complete(db, book, job, old_timestamp)
    finally:
        event.remove(Session, "before_flush", fail_flush)

    saved = db.get_book(book.abs_id)
    saved_job = db.get_latest_job(book.abs_id, kind="alignment")
    assert saved.status == "pending" and saved.transcript_file is None
    assert (saved_job.stage, saved_job.retry_count, saved_job.progress,
            saved_job.last_error, saved_job.recovery_identity) == (
        original_job.stage, original_job.retry_count, original_job.progress,
        original_job.last_error, original_job.recovery_identity,
    )
    assert _map(db) == original_map
    with db.get_session() as session:
        assert session.query(BookAlignmentBackup).filter_by(abs_id="book").count() == 0


@pytest.mark.parametrize("race", ["book", "job", "map", "hash_owner"])
def test_completion_refuses_races_without_partial_map_or_activation(db, race):
    book = _book(db)
    job = _stage(db, book)
    old_timestamp = _old_map(db)
    original_map = _map(db)
    if race == "book":
        changed = db.get_book(book.abs_id)
        changed.audio_source_id = "new-audio"
        db.save_book(changed)
    elif race == "job":
        db.update_job_by_id(job.id, recovery_identity="changed")
    elif race == "map":
        with db.get_session() as session:
            row = session.query(BookAlignment).filter_by(abs_id=book.abs_id).one()
            row.last_updated = row.last_updated.replace(year=row.last_updated.year + 1)
    else:
        with db.get_session() as session:
            session.add(KosyncDocument(
                document_hash="good-hash", filename="claimed.epub", linked_abs_id="other-book"
            ))

    assert not _complete(db, book, job, old_timestamp)
    assert _map(db) == original_map
    assert db.get_book(book.abs_id).status == "pending"
    current_job = db.get_latest_job(book.abs_id, kind="alignment")
    assert current_job.stage == "ebook_input_recovery"
    assert current_job.progress == 0.4
    with db.get_session() as session:
        assert session.query(BookAlignmentBackup).filter_by(abs_id=book.abs_id).count() == 0


@pytest.mark.parametrize("bad_map", [[], [{"char": 0, "ts": 0}],
                                      [{"char": 0, "ts": 0}, {"char": float("nan"), "ts": 1}],
                                      [{"char": 0, "ts": 0}, {"char": 10, "ts": float("inf")}],
                                      [{"char": 1, "ts": 0}, {"char": 0, "ts": 1}]])
def test_invalid_replacement_map_does_not_activate_or_backup(db, bad_map):
    book = _book(db)
    job = _stage(db, book)
    old_timestamp = _old_map(db)
    original_map = _map(db)

    assert not _complete(db, book, job, old_timestamp, alignment_map=bad_map)

    assert _map(db) == original_map
    assert db.get_book(book.abs_id).status == "pending"
    with db.get_session() as session:
        assert session.query(BookAlignmentBackup).filter_by(abs_id=book.abs_id).count() == 0
