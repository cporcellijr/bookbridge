"""SQLite transaction guards for automatic recovery of known-bad ebook inputs."""

from datetime import datetime

import pytest

from src.db.database_service import DatabaseService
from src.db.models import Book, BookAlignment, Job, KosyncDocument, State, UserBook


@pytest.fixture
def db(tmp_path):
    service = DatabaseService(str(tmp_path / "ebook-recovery.db"))
    try:
        yield service
    finally:
        service.db_manager.close()


def _book(db, abs_id="recover-me", **overrides):
    values = {
        "abs_id": abs_id,
        "abs_title": "Recovery Fixture",
        "ebook_filename": "wrong-copy.pdf",
        "original_ebook_filename": "reader-copy.epub",
        "ebook_source": "CWA",
        "ebook_source_id": "1516",
        "kosync_doc_id": "bad-pdf-hash",
        "sync_mode": "audiobook",
        "status": "active",
    }
    values.update(overrides)
    return db.save_book(Book(**values))


def _snapshot(db, abs_id="recover-me"):
    return next(candidate for candidate in db.get_ebook_input_recovery_candidates()
                if candidate["book"].abs_id == abs_id)


def _queue(db, abs_id="recover-me", **overrides):
    job = Job(
        abs_id=abs_id, last_attempt=100.0, retry_count=2, last_error="old failure",
        progress=0.4, kind="alignment", stage=None,
    )
    for key, value in overrides.items():
        setattr(job, key, value)
    db.save_job(job)
    return db.get_latest_job(abs_id, kind="alignment")


def _repair(db, candidate, *, abs_id="recover-me", filename="reader-copy.epub",
            new_hash="good-epub-hash", bad_hash="bad-pdf-hash"):
    return db.repair_ebook_input_recovery(
        abs_id,
        candidate["book"],
        candidate["job"],
        candidate["alignment_last_updated"],
        filename,
        new_hash,
        bad_kosync_doc_id=bad_hash,
    )


def test_recovery_repair_updates_catalog_and_queues_one_staged_job(db):
    book = _book(db)
    job = _queue(db)
    before = _snapshot(db)

    assert _repair(db, before) is True

    saved = db.get_book(book.abs_id)
    assert (saved.ebook_filename, saved.kosync_doc_id, saved.status) == (
        "reader-copy.epub", "good-epub-hash", "pending",
    )
    queued = db.get_latest_job(book.abs_id, kind="alignment")
    assert queued.id == job.id
    assert queued.stage == "ebook_input_recovery"
    assert queued.retry_count == 0
    assert queued.progress == 0.0
    assert queued.last_error is None


def test_recovery_relink_preserves_rows_and_leaves_unrelated_documents_alone(db):
    book = _book(db)
    first = db.create_user("reader-one")
    second = db.create_user("reader-two")
    with db.get_session() as session:
        session.add_all([
            UserBook(user_id=first.id, abs_id=book.abs_id),
            UserBook(user_id=second.id, abs_id=book.abs_id),
            State(abs_id=book.abs_id, client_name="ABS", percentage=0.63, user_id=first.id),
            State(abs_id=book.abs_id, client_name="KoSync", percentage=0.82, user_id=second.id),
            KosyncDocument(document_hash="bad-pdf-hash", filename="wrong-copy.pdf",
                           linked_abs_id=book.abs_id),
            KosyncDocument(document_hash="unrelated-hash", filename="other.epub",
                           linked_abs_id="other-book"),
        ])
    _queue(db)
    before = _snapshot(db)

    assert _repair(db, before) is True

    with db.get_session() as session:
        links = {(row.user_id, row.abs_id) for row in session.query(UserBook).all()}
        states = {(row.client_name, row.user_id, row.percentage)
                  for row in session.query(State).filter_by(abs_id=book.abs_id).all()}
        docs = {row.document_hash: row.linked_abs_id
                for row in session.query(KosyncDocument).all()}
    assert links == {(first.id, book.abs_id), (second.id, book.abs_id)}
    assert states == {("ABS", first.id, 0.63), ("KoSync", second.id, 0.82)}
    assert docs["unrelated-hash"] == "other-book"
    assert docs["bad-pdf-hash"] is None


@pytest.mark.parametrize("stale_write", ["book", "job", "alignment", "deleted"])
def test_repair_rejects_stale_snapshot_without_overwriting_newer_state(db, stale_write):
    book = _book(db)
    _queue(db)
    if stale_write == "alignment":
        with db.get_session() as session:
            session.add(BookAlignment(
                abs_id=book.abs_id, alignment_map_json='[{"char":0,"ts":0}]',
                align_method="lexical", total_chars=0,
            ))
    candidate = _snapshot(db)

    if stale_write == "book":
        changed = db.get_book(book.abs_id)
        changed.ebook_source_id = "9999"
        db.save_book(changed)
    elif stale_write == "job":
        changed = db.get_latest_job(book.abs_id, kind="alignment")
        changed.progress = 0.5
        db.update_job_by_id(changed.id, progress=changed.progress)
    elif stale_write == "alignment":
        with db.get_session() as session:
            alignment = session.query(BookAlignment).filter_by(abs_id=book.abs_id).first()
            alignment.alignment_map_json = '[{"char":0,"ts":0},{"char":10,"ts":1}]'
            alignment.total_chars = 10
    else:
        db.delete_book(book.abs_id)

    assert _repair(db, candidate) is False

    current = db.get_book(book.abs_id)
    if stale_write == "deleted":
        assert current is None
    elif stale_write == "book":
        assert current.ebook_source_id == "9999"
        assert current.kosync_doc_id == "bad-pdf-hash"
    elif stale_write == "job":
        assert db.get_latest_job(book.abs_id, kind="alignment").progress == 0.5
        assert current.kosync_doc_id == "bad-pdf-hash"
    else:
        assert db.get_alignment_total_chars(book.abs_id) == 10
        assert current.kosync_doc_id == "bad-pdf-hash"


def test_repair_declines_when_new_hash_is_already_owned_by_another_book(db):
    _book(db)
    _book(db, abs_id="other-book", ebook_filename="other.epub",
          original_ebook_filename=None, ebook_source=None, ebook_source_id=None,
          kosync_doc_id="already-owned")
    db.ensure_linked_kosync_document("already-owned", "other-book")
    _queue(db)
    candidate = _snapshot(db)

    assert _repair(db, candidate, new_hash="already-owned") is False
    current = db.get_book("recover-me")
    assert current.kosync_doc_id == "bad-pdf-hash"
    assert current.status == "active"


def test_candidate_snapshot_contains_detached_book_job_and_actual_map_timestamp(db):
    book = _book(db)
    _queue(db)
    marker = datetime(2026, 10, 13)
    with db.get_session() as session:
        session.add(BookAlignment(
            abs_id=book.abs_id, alignment_map_json='[]', align_method="linear",
            total_chars=0,
        ))
        session.query(BookAlignment).filter_by(abs_id=book.abs_id).update(
            {BookAlignment.last_updated: marker}
        )

    candidate = _snapshot(db)

    assert candidate["book"].abs_id == book.abs_id
    assert candidate["job"].id == db.get_latest_job(book.abs_id, kind="alignment").id
    assert candidate["alignment_last_updated"] == marker
    assert candidate["job"].stage is None


def test_candidate_snapshot_exposes_recovery_stage_for_idempotent_worker_gate(db):
    book = _book(db)
    _queue(db, stage="ebook_input_recovery")

    candidate = _snapshot(db)
    assert candidate["book"].abs_id == book.abs_id
    assert candidate["job"].stage == "ebook_input_recovery"


def test_completion_cas_only_activates_queued_job_with_a_usable_map(db):
    book = _book(db, ebook_filename="reader-copy.epub", kosync_doc_id="good-epub-hash",
                 status="pending")
    _queue(db, stage="ebook_input_recovery")
    with db.get_session() as session:
        session.add(BookAlignment(
            abs_id=book.abs_id, alignment_map_json='[{"char":0,"ts":0},{"char":40,"ts":4}]',
            align_method="lexical", total_chars=40,
        ))
    expected = db.get_book(book.abs_id)
    map_timestamp = db.get_ebook_input_recovery_alignment(book.abs_id)[1]

    assert db.complete_ebook_input_recovery(book.abs_id, expected, map_timestamp) is True

    saved = db.get_book(book.abs_id)
    job = db.get_latest_job(book.abs_id, kind="alignment")
    assert saved.status == "active" and saved.transcript_file == "DB_MANAGED"
    assert job.stage is None and job.progress == 1.0 and job.last_error is None


@pytest.mark.parametrize(
    "invalidator", [
        "remap", "manual_stage", "map_changed", "empty_map", "audio_identity",
        "audio_duration", "nan_anchor", "infinite_anchor", "negative_anchor",
    ]
)
def test_completion_cas_refuses_stale_or_unusable_recovery(invalidator, db):
    book = _book(db, ebook_filename="reader-copy.epub", kosync_doc_id="good-epub-hash",
                 status="pending")
    _queue(db, stage="ebook_input_recovery")
    with db.get_session() as session:
        session.add(BookAlignment(
            abs_id=book.abs_id, alignment_map_json='[]', align_method="linear",
            total_chars=40,
        ))
    expected = db.get_book(book.abs_id)
    map_timestamp = db.get_ebook_input_recovery_alignment(book.abs_id)[1]
    if invalidator == "remap":
        current = db.get_book(book.abs_id)
        current.ebook_source_id = "new-cwa-id"
        db.save_book(current)
        # Even with a valid map, the earlier worker must not activate a remapped row.
        with db.get_session() as session:
            alignment = session.query(BookAlignment).filter_by(abs_id=book.abs_id).one()
            alignment.total_chars = 40
            alignment.alignment_map_json = '[{"char":0,"ts":0},{"char":40,"ts":4}]'
    elif invalidator == "manual_stage":
        job = db.get_latest_job(book.abs_id, kind="alignment")
        db.update_job_by_id(job.id, stage="ebook_input_recovery_manual")
    elif invalidator == "map_changed":
        with db.get_session() as session:
            alignment = session.query(BookAlignment).filter_by(abs_id=book.abs_id).one()
            alignment.last_updated = map_timestamp.replace(year=map_timestamp.year + 1)
    elif invalidator == "audio_identity":
        current = db.get_book(book.abs_id)
        current.audio_source_id = "remapped-audio-id"
        db.save_book(current)
    elif invalidator == "audio_duration":
        current = db.get_book(book.abs_id)
        current.audio_duration = 123.0
        db.save_book(current)
    elif invalidator in {"nan_anchor", "infinite_anchor", "negative_anchor"}:
        malformed_maps = {
            "nan_anchor": '[{"char":0,"ts":0},{"char":NaN,"ts":4}]',
            "infinite_anchor": '[{"char":0,"ts":0},{"char":Infinity,"ts":4}]',
            "negative_anchor": '[{"char":0,"ts":0},{"char":-1,"ts":4}]',
        }
        with db.get_session() as session:
            alignment = session.query(BookAlignment).filter_by(abs_id=book.abs_id).one()
            alignment.alignment_map_json = malformed_maps[invalidator]
            alignment.last_updated = map_timestamp

    assert db.complete_ebook_input_recovery(book.abs_id, expected, map_timestamp) is False
    current = db.get_book(book.abs_id)
    assert current.status == "pending"
    if invalidator == "manual_stage":
        assert db.get_latest_job(book.abs_id, kind="alignment").stage == "ebook_input_recovery_manual"
