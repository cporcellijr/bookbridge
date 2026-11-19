"""Remote alignment workers: claims, leases, the /api/v1/worker blueprint, and a
main instance and worker run end to end in one process.

See src/services/alignment_workers.py (main side), src/api/alignment_worker_api.py
and src/alignment_worker.py (the worker).
"""
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from flask import Flask

from src.api import admin_api, alignment_worker_api
from src.db.database_service import DatabaseService
from src.db.models import JOB_KIND_ALIGNMENT, Book, BookAlignment, BookAlignmentBackup, Job
from src.services import alignment_workers
from src.services.alignment_service import AlignmentService
from src.services.alignment_workers import AlignmentWorkerCoordinator, remote_eligible
from src.services.job_queue_view import build_job_queue_view
from src.services.segment_fit import Segment
from src.sync_manager import SyncManager
from src.utils.ebook_utils import EbookParser
from src.utils.polisher import Polisher

TOKEN = "w" * 40


@pytest.fixture
def db(tmp_path):
    service = DatabaseService(str(tmp_path / "main.db"))
    yield service
    service.db_manager.close()


def _book(db, abs_id, status="pending", position=None, **fields):
    book = Book(abs_id=abs_id, abs_title=f"Title {abs_id}", ebook_filename=f"{abs_id}.epub",
                audio_source="ABS", audio_source_id=abs_id)
    book.status = status
    book.sync_mode = fields.pop("sync_mode", "audiobook")
    for key, value in fields.items():
        setattr(book, key, value)
    db.save_book(book)
    if position is not None:
        with db.get_session() as session:
            session.query(Book).filter(Book.abs_id == abs_id).update({Book.alignment_queue_position: position})
    return book


def _claim(db, owner="desk", lease=300, now=None, eligible=remote_eligible, **kwargs):
    return db.claim_next_alignment(eligible=eligible, owner=owner, lease_seconds=lease,
                                   max_retries=5, retry_delay_mins=15, now=now, **kwargs)


# --- claims ---------------------------------------------------------------------


def test_claims_follow_the_job_queue_order(db):
    _book(db, "a")
    _book(db, "b", position=2.0)
    _book(db, "c", position=1.0)
    order = [db.claim_next_alignment().book.abs_id for _ in range(3)]
    assert order == ["c", "b", "a"]
    assert db.claim_next_alignment() is None
    assert all(db.get_book(x).status == "processing" for x in "abc")


def test_claims_follow_a_job_queue_move(db):
    """move_pending_alignment_book's reordering applies to remote claims unchanged."""
    from src.db.models import User, UserBook
    with db.get_session() as session:
        session.add(User(username="reader", password_hash="x", role="user"))
    uid = db.get_user_by_username("reader").id
    for abs_id in ("a", "b", "c"):
        _book(db, abs_id, user_id=uid)
        if not db.get_book_user_ids(abs_id):
            with db.get_session() as session:
                session.add(UserBook(user_id=uid, abs_id=abs_id))
    assert db.move_pending_alignment_book(uid, "c", "top") == "moved"
    assert _claim(db).book.abs_id == "c"
    assert _claim(db).book.abs_id == "a"


def test_concurrent_claims_never_return_the_same_book(db):
    for i in range(3):
        _book(db, f"book-{i}")
    results, barrier = [], threading.Barrier(8)

    def claim():
        barrier.wait()
        claimed = _claim(db, owner=f"w{threading.get_ident()}")
        results.append(claimed.book.abs_id if claimed else None)

    threads = [threading.Thread(target=claim) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    taken = [r for r in results if r]
    assert sorted(taken) == ["book-0", "book-1", "book-2"]


def test_a_conditional_flip_loses_to_a_book_that_changed_status(db):
    _book(db, "a")
    assert db.claim_alignment_book("a", "failed_retry_later") is False
    assert db.claim_alignment_book("a", "pending") is True
    assert db.claim_alignment_book("a", "pending") is False


def test_retry_eligibility_matches_the_local_rules(db):
    now = time.time()
    _book(db, "fresh", status="failed_retry_later")
    db.save_job(Job(abs_id="fresh", last_attempt=now - 60, retry_count=1))
    _book(db, "spent", status="failed_retry_later")
    db.save_job(Job(abs_id="spent", last_attempt=now - 3600, retry_count=5))
    _book(db, "manual", status="failed_retry_later")
    db.save_job(Job(abs_id="manual", last_attempt=now - 3600, retry_count=1,
                    stage="ebook_input_recovery_manual"))
    _book(db, "due", status="failed_retry_later")
    db.save_job(Job(abs_id="due", last_attempt=now - 3600, retry_count=2))

    claim = _claim(db, now=now)
    assert claim.book.abs_id == "due" and claim.is_retry
    assert claim.job.retry_count == 2  # a retry keeps its attempt count
    assert _claim(db, now=now) is None


def test_retries_wait_while_anything_is_pending(db):
    _book(db, "due", status="failed_retry_later")
    db.save_job(Job(abs_id="due", last_attempt=0, retry_count=1))
    _book(db, "ebook", sync_mode="ebook_only")
    assert _claim(db) is None  # only an ebook-only book pending: no retry this tick
    assert db.claim_next_alignment(allow_retries=False) is None


def test_remote_workers_are_offered_full_pipeline_books_only(db):
    _book(db, "ebook", sync_mode="ebook_only", position=1.0)
    _book(db, "audio", sync_mode="audiobook_only", position=2.0)
    _book(db, "story", position=3.0, storyteller_uuid="0000-1111")
    _book(db, "recover", position=4.0)
    db.save_job(Job(abs_id="recover", last_attempt=time.time(), stage="ebook_input_recovery",
                    recovery_identity="x"))
    _book(db, "plain", position=5.0)
    assert _claim(db).book.abs_id == "plain"
    assert _claim(db) is None
    # Without the remote filter the same claim takes the Storyteller and recovery books.
    assert db.claim_next_alignment().book.abs_id == "story"
    assert db.claim_next_alignment().book.abs_id == "recover"


def test_a_remote_claim_writes_its_lease(db):
    _book(db, "a", position=3.0)
    claim = _claim(db, owner="desk", lease=120, now=1000.0)
    job = db.get_latest_job("a", kind=JOB_KIND_ALIGNMENT)
    assert job.lease_id == claim.lease_id and len(job.lease_id) == 32
    assert job.lease_owner == "desk"
    assert job.lease_expires_at == 1120.0
    assert job.queue_position_at_claim == 3.0
    assert db.get_book("a").alignment_queue_position is None


def _local_manager(db):
    manager = SyncManager.__new__(SyncManager)
    manager.database_service = db
    manager._job_thread = None
    manager.library_service = None
    manager.user_client_registry = None
    manager._recover_ebook_input_candidates = lambda: None
    return manager


def _local_tick(db):
    """One check_pending_jobs tick; the book the local job started, or None."""
    with patch("src.sync_manager.threading.Thread") as thread_cls:
        _local_manager(db).check_pending_jobs()
    return thread_cls.call_args.kwargs["args"][0].abs_id if thread_cls.called else None


def test_the_local_job_leaves_worker_books_to_workers_when_told(db, monkeypatch):
    monkeypatch.setenv("ALIGNMENT_LOCAL_WORKER", "false")
    monkeypatch.setenv("ALIGNMENT_WORKER_TOKEN", TOKEN)
    _book(db, "plain", position=1.0)
    _book(db, "story", position=2.0, transcript_source="storyteller")
    assert _local_tick(db) == "story"
    assert db.get_book("plain").status == "pending"
    assert _claim(db).book.abs_id == "plain"


def test_the_local_flag_is_ignored_without_a_worker_api(db, monkeypatch):
    monkeypatch.setenv("ALIGNMENT_LOCAL_WORKER", "false")
    monkeypatch.setenv("ALIGNMENT_WORKER_TOKEN", "short")
    _book(db, "plain")
    assert _local_tick(db) == "plain"


def test_the_local_job_keeps_the_attempt_count_of_an_expired_lease(db):
    _book(db, "a")
    _claim(db, lease=60, now=1000.0)
    db.expire_alignment_leases(max_retries=5, now=1061.0)
    assert _local_tick(db) == "a"
    assert db.get_latest_job("a", kind=JOB_KIND_ALIGNMENT).retry_count == 1


# --- leases -----------------------------------------------------------------------


def test_an_expired_lease_returns_the_book_to_its_old_position_as_an_attempt(db):
    _book(db, "a", position=1.0)
    _book(db, "b", position=2.0)
    claim = _claim(db, lease=60, now=1000.0)
    assert claim.book.abs_id == "a"
    _book(db, "c", position=3.0)

    assert db.expire_alignment_leases(max_retries=5, now=1059.0) == []
    assert db.expire_alignment_leases(max_retries=5, now=1061.0) == [("a", "desk")]
    book = db.get_book("a")
    assert book.status == "pending" and book.alignment_queue_position == 1.0
    job = db.get_latest_job("a", kind=JOB_KIND_ALIGNMENT)
    assert job.retry_count == 1
    assert job.last_error == "Worker lease expired (desk)"
    assert job.lease_id is None
    # Back at the head of the queue, ahead of newer books, with its count kept.
    again = _claim(db, now=1100.0)
    assert again.book.abs_id == "a" and again.job.retry_count == 1


def test_a_book_that_kills_every_worker_ends_failed_permanent(db):
    _book(db, "a")
    now = 1000.0
    for attempt in range(1, 6):
        assert _claim(db, lease=60, now=now).book.abs_id == "a"
        now += 61
        assert db.expire_alignment_leases(max_retries=5, now=now) == [("a", "desk")]
        now += 1
    book = db.get_book("a")
    assert book.status == "failed_permanent"
    job = db.get_latest_job("a", kind=JOB_KIND_ALIGNMENT)
    assert job.retry_count == 5
    assert "max retries" in job.last_error
    assert _claim(db, now=now) is None


def test_a_lease_is_fenced_once_it_stops_being_current(db):
    _book(db, "a")
    claim = _claim(db, lease=60, now=1000.0)
    lid = claim.lease_id
    assert db.get_alignment_lease(lid, now=1001.0) is not None
    assert db.renew_alignment_lease(lid, 60, progress=0.4, now=1050.0)
    assert db.get_alignment_lease(lid, now=1100.0) is not None  # renewed to 1110
    assert db.get_latest_job("a", kind=JOB_KIND_ALIGNMENT).progress == 0.4
    assert db.get_alignment_lease(lid, now=1111.0) is None       # expired
    assert db.renew_alignment_lease(lid, 60, now=1111.0) is False
    assert db.get_alignment_lease("nope") is None


def test_a_newer_job_or_a_deleted_book_fences_the_lease(db):
    _book(db, "a")
    lid = _claim(db).lease_id
    db.save_job(Job(abs_id="a", last_attempt=time.time() + 1))  # e.g. a Remap
    assert db.get_alignment_lease(lid) is None
    _book(db, "b")
    lid = _claim(db).lease_id
    db.delete_book("b")
    assert db.get_alignment_lease(lid) is None


def test_restart_cleanup_leaves_a_workers_book_alone(db, tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    _book(db, "remote")
    _claim(db)
    _book(db, "local")
    db.claim_next_alignment()  # a local job, interrupted by the restart
    manager = SyncManager.__new__(SyncManager)
    manager.database_service = db
    manager.alignment_service = AlignmentService(db, Polisher())
    manager.cleanup_stale_jobs()
    assert db.get_book("remote").status == "processing"
    assert db.get_book("local").status == "failed_retry_later"


def test_the_job_queue_names_the_worker(db):
    _book(db, "a")
    _claim(db, owner="desk")
    view = build_job_queue_view([], [], [db.get_book("a")], db.get_all_jobs(), [], time.time())
    running = [e for e in view["running"] if e.get("book_id") == "a"]
    assert running and running[0]["detail"] == "Aligning on worker desk"


# --- the API ------------------------------------------------------------------------


class _Coordinator:
    """Records calls; the real coordinator is exercised further down."""

    def __init__(self):
        self.claims = []

    def claim(self, worker, lease_seconds):
        self.claims.append((worker, lease_seconds))
        return None


@pytest.fixture
def api_client(monkeypatch):
    app = Flask(__name__)
    app.register_blueprint(alignment_worker_api.alignment_worker_api_bp)
    app.register_blueprint(admin_api.admin_api_bp)
    coordinator = _Coordinator()
    alignment_worker_api.init_alignment_worker_api(coordinator)
    admin_api.init_admin_api(object())
    admin_api._AUTH_FAILURES.clear()
    yield app.test_client(), coordinator, monkeypatch
    alignment_worker_api.init_alignment_worker_api(None)
    admin_api.init_admin_api(None)
    admin_api._AUTH_FAILURES.clear()


def _auth(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


def test_worker_routes_do_not_exist_without_a_token(api_client):
    client, coordinator, monkeypatch = api_client
    monkeypatch.delenv("ALIGNMENT_WORKER_TOKEN", raising=False)
    assert client.post("/api/v1/worker/claim", json={"worker": "w"}, headers=_auth()).status_code == 404
    monkeypatch.setenv("ALIGNMENT_WORKER_TOKEN", "too-short")
    assert client.post("/api/v1/worker/claim", json={"worker": "w"}, headers=_auth("too-short")).status_code == 404
    assert coordinator.claims == []


def test_worker_routes_need_the_token(api_client):
    client, coordinator, monkeypatch = api_client
    monkeypatch.setenv("ALIGNMENT_WORKER_TOKEN", TOKEN)
    assert client.post("/api/v1/worker/claim", json={"worker": "w"}).status_code == 401
    assert client.post("/api/v1/worker/claim", json={"worker": "w"},
                       headers=_auth("x" * 40)).status_code == 401
    response = client.post("/api/v1/worker/claim", json={"worker": "desk", "lease_seconds": 90},
                           headers=_auth())
    assert response.status_code == 204
    assert coordinator.claims == [("desk", 90)]
    assert client.post("/api/v1/worker/claim", json={"worker": "no spaces"},
                       headers=_auth()).status_code == 400


def test_the_worker_token_does_not_open_the_admin_api(api_client):
    client, _coordinator, monkeypatch = api_client
    monkeypatch.setenv("ALIGNMENT_WORKER_TOKEN", TOKEN)
    monkeypatch.setenv("ADMIN_API_TOKEN", "a" * 40)
    assert client.get("/api/v1/admin/users", headers=_auth()).status_code == 401


def test_the_worker_token_does_not_open_kosync():
    from src.api import kosync_server
    source = Path(kosync_server.__file__).read_text(encoding="utf-8")
    assert "ALIGNMENT_WORKER_TOKEN" not in source and "alignment_workers" not in source


# --- the coordinator: inputs, results, failures -------------------------------------------


def _make_epub(path: Path, paragraphs: list[str]) -> Path:
    from ebooklib import epub

    book = epub.EpubBook()
    book.set_identifier("worker-test")
    book.set_title("Worker Test")
    book.set_language("en")
    chapters = []
    for i, text in enumerate(paragraphs):
        chapter = epub.EpubHtml(title=f"Chapter {i + 1}", file_name=f"ch{i + 1}.xhtml", lang="en")
        chapter.content = f"<html><body><h1>Chapter {i + 1}</h1><p>{text}</p></body></html>"
        book.add_item(chapter)
        chapters.append(chapter)
    book.toc = chapters
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav"] + chapters
    epub.write_epub(str(path), book)
    return path


_WORDS = ("the river ran past the old mill where martha kept her bees and the "
          "miller's boy watched the water turn the wheel through every season").split()


def _paragraphs(count=4, words=180):
    out = []
    for c in range(count):
        out.append(" ".join(f"{_WORDS[(c * 7 + i) % len(_WORDS)]}" + ("" if i % 9 else f" w{c}x{i}")
                            for i in range(words)))
    return out


class _Library:
    cwa_client = None

    def __init__(self, epub_path):
        self.epub_path = epub_path

    def acquire_ebook(self, item_details, book):
        return str(self.epub_path)


class _Audio:
    def __init__(self, paths):
        self.paths = paths

    def get_audio_files(self, source_id, bridge_key=None):
        return [{"local_path": str(p), "ext": "mp3"} for p in self.paths]

    def get_chapters(self, source_id):
        return [{"start": 0, "end": 60, "title": "Chapter 1"}]


@pytest.fixture
def main(tmp_path, monkeypatch, db):
    """A main instance: real database, alignment service and SyncManager."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "main-data"))
    monkeypatch.setenv("CTC_ENABLED", "false")
    monkeypatch.setenv("ALIGNMENT_WORKER_TOKEN", TOKEN)
    books_dir = tmp_path / "books"
    books_dir.mkdir()
    epub_path = _make_epub(books_dir / "river.epub", _paragraphs())
    audio = [tmp_path / "part1.mp3", tmp_path / "part2.mp3"]
    for i, part in enumerate(audio):
        part.write_bytes(bytes([i]) * (300_000 + i))
    parser = EbookParser(str(books_dir), epub_cache_dir=tmp_path / "epub_cache")
    manager = SyncManager(
        database_service=db, alignment_service=AlignmentService(db, Polisher()),
        ebook_parser=parser, library_service=_Library(epub_path),
        audio_source_adapters={"ABS": _Audio(audio)}, sync_clients={},
        data_dir=tmp_path / "main-data", epub_cache_dir=tmp_path / "epub_cache", books_dir=books_dir,
    )
    coordinator = AlignmentWorkerCoordinator(manager, db)
    book = _book(db, "river", ebook_filename="river.epub", audio_duration=600.0)
    return SimpleNamespace(db=db, manager=manager, coordinator=coordinator, book=book,
                           epub_path=epub_path, audio=audio, tmp=tmp_path)


def _prepared(main, owner="desk"):
    claimed = main.coordinator.claim(owner, 300)
    for _ in range(200):
        state, manifest = main.coordinator.inputs(claimed["lease_id"])
        if state != "preparing":
            break
        time.sleep(0.05)
    assert state == "ready", state
    return claimed, manifest


def test_llm_alignment_switches_follow_the_main_instance_but_not_its_endpoint(main, monkeypatch):
    monkeypatch.setenv("OLLAMA_ENABLED", "true")
    monkeypatch.setenv("OLLAMA_ALIGN_CONTENT_GUARD", "false")
    monkeypatch.setenv("OLLAMA_URL", "http://main-only-ollama:11434")
    monkeypatch.setenv("LLM_API_KEY", "sk-not-for-workers")
    _, manifest = _prepared(main)
    assert manifest["settings"]["OLLAMA_ENABLED"] == "true"
    assert manifest["settings"]["OLLAMA_ALIGN_CONTENT_GUARD"] == "false"
    assert "OLLAMA_URL" not in manifest["settings"]
    assert "LLM_API_KEY" not in manifest["settings"]


def test_inputs_are_resolved_on_the_main_instance_and_served_by_index(main):
    claimed, manifest = _prepared(main)
    assert manifest["book"]["abs_id"] == "river"
    assert manifest["settings"]["CTC_ENABLED"] == "false"
    assert [f["role"] for f in manifest["files"]] == ["epub", "audio", "audio"]
    assert manifest["files"][2]["size"] == 300_001
    assert manifest["existing_alignment"] is None
    # The KOSync id is locked from the served EPUB before any worker sees it.
    book = main.db.get_book("river")
    assert book.kosync_doc_id == main.manager.ebook_parser.get_kosync_id(main.epub_path)
    assert manifest["book"]["kosync_doc_id"] == book.kosync_doc_id
    assert main.coordinator.file_path(claimed["lease_id"], 0) == main.epub_path
    assert main.coordinator.file_path(claimed["lease_id"], 2) == main.audio[1]
    with pytest.raises(IndexError):
        main.coordinator.file_path(claimed["lease_id"], 3)
    assert "secret" not in json.dumps(manifest).lower()


def test_an_input_failure_goes_through_the_retry_logic(main):
    main.manager.library_service = _Library(main.tmp / "missing.epub")
    main.manager._get_local_epub = lambda name: None
    main.manager.library_service.acquire_ebook = lambda item, book: None
    claimed = main.coordinator.claim("desk", 300)
    for _ in range(200):
        if main.db.get_book("river").status != "processing":
            break
        time.sleep(0.05)
    assert main.db.get_book("river").status == "failed_retry_later"
    job = main.db.get_latest_job("river", kind=JOB_KIND_ALIGNMENT)
    assert job.retry_count == 1 and "Could not locate" in job.last_error
    assert main.coordinator.inputs(claimed["lease_id"]) == ("gone", None)


def _points(n=40, chars=4000, seconds=600.0):
    return [{"char": int(i * chars / (n - 1)), "ts": round(i * seconds / (n - 1), 3)} for i in range(n)]


def test_a_result_is_published_through_publish_map_and_finishes_the_book(main):
    claimed, _manifest = _prepared(main)
    calls = []
    real_publish = main.manager.alignment_service._publish_map

    def spy(abs_id, alignment_map, align_method, **kwargs):
        calls.append((abs_id, align_method, kwargs))
        return real_publish(abs_id, alignment_map, align_method, **kwargs)

    main.manager.alignment_service._publish_map = spy
    segment = {"char_start": 0, "char_end": 4000, "ts_start": -1.5, "ts_end": 600.0,
               "inliers": 30, "residual": 0.2}
    outcome = main.coordinator.complete(claimed["lease_id"], {
        "alignment_map": _points(), "align_method": "ctc", "total_chars": 4000,
        "exclude_spans": [[10, 20]], "segments": [segment],
        "transcript_source": "ctc", "epub_sha256": _manifest["files"][0]["sha256"],
    })
    assert outcome == {"status": "published"}
    (abs_id, method, kwargs), = calls
    assert (abs_id, method, kwargs["total_chars"]) == ("river", "ctc", 4000)
    assert kwargs["exclude_spans"] == [(10, 20)]
    assert kwargs["segments"] == [Segment(**segment)]
    book = main.db.get_book("river")
    assert book.status == "active" and book.transcript_source == "ctc"
    job = main.db.get_latest_job("river", kind=JOB_KIND_ALIGNMENT)
    assert job.progress == 1.0 and job.retry_count == 0
    assert main.db.get_alignment_method("river") == "ctc"
    # The lease ended with the job: a late heartbeat or second upload is fenced.
    assert main.coordinator.heartbeat(claimed["lease_id"], 0.5) is False
    with pytest.raises(alignment_workers.LeaseGone):
        main.coordinator.complete(claimed["lease_id"], {"alignment_map": None})


def test_a_vetoed_result_keeps_the_incumbent_map(main):
    with main.db.get_session() as session:
        session.add(BookAlignment(abs_id="river", alignment_map_json=json.dumps(_points()),
                                  align_method="ctc", total_chars=4000))
    claimed, manifest = _prepared(main)
    assert manifest["existing_alignment"]["align_method"] == "ctc"
    with patch.object(main.manager.alignment_service, "_publish_map", return_value=False) as publish:
        outcome = main.coordinator.complete(claimed["lease_id"], {
            "alignment_map": _points(5), "align_method": "lexical", "total_chars": 4000,
            "epub_sha256": manifest["files"][0]["sha256"],
        })
    publish.assert_called_once()
    assert outcome == {"status": "vetoed"}
    assert main.db.get_book("river").status == "active"
    assert main.db.get_alignment_method("river") == "ctc"


def test_a_real_publish_backs_up_the_map_it_replaces(main):
    with main.db.get_session() as session:
        session.add(BookAlignment(abs_id="river", alignment_map_json=json.dumps(_points(2)),
                                  align_method="linear", total_chars=4000))
    claimed, manifest = _prepared(main)
    assert main.coordinator.complete(claimed["lease_id"], {
        "alignment_map": _points(), "align_method": "ctc", "total_chars": 4000,
        "epub_sha256": manifest["files"][0]["sha256"],
    }) == {"status": "published"}
    with main.db.get_session() as session:
        backup = session.query(BookAlignmentBackup).filter_by(abs_id="river").one()
        assert backup.align_method == "linear"


def test_a_result_for_a_different_epub_is_refused(main):
    claimed, _ = _prepared(main)
    with pytest.raises(ValueError, match="EPUB"):
        main.coordinator.complete(claimed["lease_id"], {
            "alignment_map": _points(), "align_method": "ctc", "epub_sha256": "f" * 64,
        })
    assert main.db.get_book("river").status == "processing"


@pytest.mark.parametrize("error_class, status, retries", [
    ("ContentMatchRejected", "failed_permanent", 1),
    ("EbookInputError", "failed_permanent", 1),
    ("RuntimeError", "failed_retry_later", 1),
])
def test_worker_failures_use_the_local_retry_logic(main, error_class, status, retries):
    claimed, _ = _prepared(main)
    main.coordinator.fail(claimed["lease_id"], {"error_class": error_class, "message": "boom"})
    assert main.db.get_book("river").status == status
    job = main.db.get_latest_job("river", kind=JOB_KIND_ALIGNMENT)
    assert job.retry_count == retries
    assert job.last_error == "boom (on worker desk)"
    with pytest.raises(alignment_workers.LeaseGone):
        main.coordinator.fail(claimed["lease_id"], {"error_class": "RuntimeError"})


def test_a_deleted_book_cancels_the_worker_through_its_heartbeat(main):
    claimed, _ = _prepared(main)
    assert main.coordinator.heartbeat(claimed["lease_id"], 0.2) is True
    main.db.delete_book("river")
    assert main.coordinator.heartbeat(claimed["lease_id"], 0.3) is False
    with pytest.raises(alignment_workers.LeaseGone):
        main.coordinator.file_path(claimed["lease_id"], 0)


# --- end to end: a main instance and a worker over HTTP -----------------------------------------


@pytest.fixture
def served(main):
    from werkzeug.serving import make_server

    app = Flask(__name__)
    app.register_blueprint(alignment_worker_api.alignment_worker_api_bp)
    alignment_worker_api.init_alignment_worker_api(main.coordinator)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield SimpleNamespace(main=main, url=f"http://127.0.0.1:{server.server_port}")
    server.shutdown()
    alignment_worker_api.init_alignment_worker_api(None)


def _fake_transcript(book_text):
    """Timed segments that read the book back, standing in for Whisper."""
    words = book_text.split()
    segments, t = [], 0.0
    for i in range(0, len(words), 20):
        chunk = words[i:i + 20]
        segments.append({"start": t, "end": t + 0.4 * len(chunk), "text": " ".join(chunk),
                         "words": [{"word": w, "start": t + 0.4 * j, "end": t + 0.4 * (j + 1)}
                                   for j, w in enumerate(chunk)]})
        t += 0.4 * len(chunk)
    return segments


def _worker(served, name="desk"):
    from src.alignment_worker import AlignmentWorker, MainClient
    return AlignmentWorker(MainClient(served.url, TOKEN), name=name,
                           data_dir=served.main.tmp / f"worker-{name}", lease_seconds=60, poll_seconds=0.1)


def test_a_worker_aligns_a_book_end_to_end(served):
    from src.utils.transcriber import AudioTranscriber

    def process_audio(self, abs_id, audio_files, full_book_text=None, **kwargs):
        assert [Path(f["local_path"]).stat().st_size for f in audio_files] == [300_000, 300_001]
        return _fake_transcript(full_book_text)

    worker = _worker(served)
    claimed = worker.client.claim(worker.name, 60)
    with patch.object(AudioTranscriber, "process_audio", process_audio), \
            patch.object(AudioTranscriber, "transcribe_from_smil", return_value=None):
        worker.run_job(claimed)

    db = served.main.db
    book = db.get_book("river")
    assert book.status == "active"
    assert book.transcript_source == "whisper"
    assert db.get_alignment_method("river")
    alignment = served.main.manager.alignment_service._get_alignment("river")
    assert alignment and len(alignment) > 10
    assert db.get_latest_job("river", kind=JOB_KIND_ALIGNMENT).progress == 1.0
    assert not (worker.scratch_root / claimed["lease_id"]).exists()  # scratch deleted


def test_a_worker_reports_a_pipeline_failure(served):
    from src.utils.transcriber import AudioTranscriber

    worker = _worker(served)
    claimed = worker.client.claim(worker.name, 60)
    with patch.object(AudioTranscriber, "process_audio", return_value=None), \
            patch.object(AudioTranscriber, "transcribe_from_smil", return_value=None):
        worker.run_job(claimed)
    book = served.main.db.get_book("river")
    assert book.status == "failed_retry_later"
    job = served.main.db.get_latest_job("river", kind=JOB_KIND_ALIGNMENT)
    assert "Failed to generate transcript" in job.last_error and "(on worker desk)" in job.last_error


def test_a_killed_worker_loses_the_book_to_the_next_one(served):
    """Worker one claims and dies; the lease expires; worker two completes the book."""
    from src.utils.transcriber import AudioTranscriber

    db = served.main.db
    dead = _worker(served, "dead")
    first = dead.client.claim(dead.name, 60)  # ...and never heartbeats again
    with db.get_session() as session:  # sixty seconds pass
        session.query(Job).filter(Job.lease_id == first["lease_id"]).update(
            {Job.lease_expires_at: time.time() - 1})
    assert db.expire_alignment_leases(max_retries=5) == [("river", "dead")]
    assert db.get_book("river").status == "pending"
    # The dead worker is fenced if it comes back.
    from src.alignment_worker import LeaseLost
    with pytest.raises(LeaseLost):
        dead.client.heartbeat(first["lease_id"], 0.5, 60)

    live = _worker(served, "live")
    second = live.client.claim(live.name, 60)
    assert second["abs_id"] == "river"
    with patch.object(AudioTranscriber, "process_audio",
                      lambda self, abs_id, files, full_book_text=None, **kw: _fake_transcript(full_book_text)), \
            patch.object(AudioTranscriber, "transcribe_from_smil", return_value=None):
        live.run_job(second)
    assert db.get_book("river").status == "active"
    jobs = [j for j in db.get_jobs_for_book("river") if j.kind == JOB_KIND_ALIGNMENT]
    assert any(j.last_error == "Worker lease expired (dead)" for j in jobs)


# --- schema ----------------------------------------------------------------------------


def test_the_lease_migration_matches_the_model(db):
    import sqlalchemy as sa
    from src.db.models import Base

    migrated = sa.inspect(db.db_manager.engine)
    mig_cols = {c["name"]: (str(c["type"]), c["nullable"]) for c in migrated.get_columns("jobs")}
    engine = sa.create_engine("sqlite:///:memory:")
    try:
        Base.metadata.create_all(engine)
        built = {c["name"]: (str(c["type"]), c["nullable"]) for c in sa.inspect(engine).get_columns("jobs")}
    finally:
        engine.dispose()
    for column in ("lease_owner", "lease_id", "lease_expires_at", "queue_position_at_claim", "lease_inputs_json"):
        assert mig_cols[column] == built[column]
        assert mig_cols[column][1] is True
    assert "ix_jobs_lease_id" in {ix["name"] for ix in migrated.get_indexes("jobs")}


def test_a_worker_marks_itself_alive_for_the_healthcheck(tmp_path, monkeypatch):
    """A worker serves no HTTP port; the image's healthcheck reads this file's age."""
    from src import alignment_worker

    alive = tmp_path / "alive"
    monkeypatch.setattr(alignment_worker, "ALIVE_FILE", alive)
    beat = alignment_worker.Heartbeat(client=None, lease_id="x", lease_seconds=600,
                                      progress=lambda: 0.0, on_lost=lambda: None)
    beat.start()
    beat.stop()
    beat.join(timeout=5)
    assert alive.is_file()
