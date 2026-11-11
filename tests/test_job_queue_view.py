"""Phase 1 Job Queue: visibility, worker handoff, auth and EPUB3 intent."""
import html
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from src.db.database_service import DatabaseService
from src.db.models import Book, Job, JOB_KIND_READALONG
from src.services.job_queue_view import build_job_queue_view
from src.utils.user_context import set_current_user_id, reset_current_user_id
from tests.test_webserver import MockContainer


class JobQueueSnapshotTests(unittest.TestCase):
    def test_staged_order_and_epub3_request_do_not_imply_dispatch(self):
        data = build_job_queue_view(
            [{"abs_title": "First", "readalong_epub_requested": True},
             {"abs_title": "Second"}],
            [], [], [], set(),
        )
        self.assertEqual([entry["title"] for entry in data["pending"]], ["First", "Second"])
        self.assertEqual([entry["order"] for entry in data["pending"]], [1, 2])
        self.assertEqual([entry["status"] for entry in data["pending"]], ["staged", "staged"])
        self.assertTrue(data["pending"][0]["epub3"])
        self.assertFalse(data["pending"][1]["epub3"])

    def test_submitted_batch_order_only_applies_to_its_own_items(self):
        data = build_job_queue_view([], [{
            "label": "Match batch", "current": 1,
            "items": [{"abs_title": "One"}, {"abs_title": "Two"}, {"abs_title": "Three"}],
        }], [], [], set())
        self.assertEqual([x["title"] for x in data["pending"]], ["Three"])
        self.assertEqual([x["order"] for x in data["pending"]], [3])
        self.assertEqual(data["running"][0]["title"], "Two")
        self.assertEqual(data["running"][0]["order"], 2)

    def test_readalong_unstarted_intent_running_and_restart_orphan(self):
        now = time.time()
        books = [
            Book(abs_id="intent", abs_title="Intent Book", status="pending"),
            Book(abs_id="live", abs_title="Live Book", status="active"),
            Book(abs_id="orphan", abs_title="Orphan Book", status="active"),
        ]
        books[0].readalong_epub_requested = True
        jobs = [
            Job(abs_id="live", last_attempt=now, kind=JOB_KIND_READALONG,
                progress=0.42, stage="transcoding_audio"),
            Job(abs_id="orphan", last_attempt=now, kind=JOB_KIND_READALONG,
                progress=0.1, stage="queued"),
        ]
        data = build_job_queue_view([], [], books, jobs, {"live"}, now=now)
        self.assertIn("after alignment finishes", next(
            x["detail"] for x in data["pending"] if x["title"] == "Intent Book"
            and x["operation"] == "EPUB3 Read-along"))
        self.assertEqual(next(x["percent"] for x in data["running"]
                              if x["title"] == "Live Book"), 42)
        orphan = next(x for x in data["recent"] if x["title"] == "Orphan Book")
        self.assertEqual(orphan["status"], "interrupted")
        self.assertFalse(any(x["title"] == "One" for x in data["pending"] + data["running"]))

    def test_readalong_warning_at_completion_and_recent_window(self):
        now = time.time()
        book = Book(abs_id="readalong", abs_title="Book")
        job = Job(abs_id="readalong", last_attempt=now, kind=JOB_KIND_READALONG,
                  progress=1.0, last_error="Delivery not confirmed")
        data = build_job_queue_view([], [], [book], [job], set(), now=now)
        self.assertEqual(data["recent"][0]["status"], "completed")
        self.assertIn("Delivery not confirmed", data["recent"][0]["detail"])
        job.last_attempt = now - 8 * 86400
        self.assertEqual(build_job_queue_view([], [], [book], [job], set(), now=now)["recent"], [])

    def test_does_not_include_jobs_for_unclaimed_books(self):
        book = Book(abs_id="own", abs_title="Own Book", status="pending")
        stranger_job = Job(abs_id="private", last_attempt=time.time(),
                           kind=JOB_KIND_READALONG, progress=0.7)
        data = build_job_queue_view([], [], [book], [stranger_job], {"private"})
        self.assertNotIn("private", str(data))

    def test_completed_alignment_row_has_no_preflight(self):
        book = Book(abs_id="completed", abs_title="Completed Book", status="active")
        job = Job(abs_id="completed", last_attempt=time.time(), kind="alignment", progress=1.0)
        preflights = {"completed": {"assessment": "uncalibrated", "epub_tokens_per_audio_hour": 7000}}
        data = build_job_queue_view([], [], [book], [job], set(), preflights=preflights)
        recent = data.get("recent", [])
        self.assertEqual(len(recent), 1)
        entry = recent[0]
        self.assertEqual(entry["title"], "Completed Book")
        self.assertNotIn("preflight", entry)


class JobQueueRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.orig_env = {key: os.environ.get(key) for key in
                         ("DATA_DIR", "BOOKS_DIR", "TEMPLATE_DIR", "STATIC_DIR")}
        project = Path(__file__).parent.parent
        os.environ.update({
            "DATA_DIR": self.tmp, "BOOKS_DIR": self.tmp,
            "TEMPLATE_DIR": str(project / "templates"),
            "STATIC_DIR": str(project / "static"),
        })
        self.db = DatabaseService(os.path.join(self.tmp, "jobs.db"))
        self.db.create_user("admin", "pw-admin", role="admin")
        self.alice = self.db.create_user("alice", "pw-alice", role="user")
        self.bob = self.db.create_user("bob", "pw-bob", role="user")
        container = MockContainer()
        container.mock_database_service = self.db
        self.init_patch = patch(
            "src.db.migration_utils.initialize_database", return_value=self.db)
        self.init_patch.start()
        from src.web_server import create_app
        self.app, _ = create_app(test_container=container)
        self.app.config.update(TESTING=True, LOGIN_DISABLED=False)
        self.client = self.app.test_client()

    def tearDown(self):
        import src.web_server as ws
        with ws._VISIBLE_BATCH_LOCK:
            ws._VISIBLE_BATCHES.clear()
        self.init_patch.stop()
        for key, value in self.orig_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _assert_reorder_warning(self, data, message):
        response = self.client.post('/job-queue/reorder', data=data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers['Location'], '/job-queue')
        page = self.client.get(response.headers['Location'])
        self.assertEqual(page.status_code, 200)
        content = html.unescape(page.get_data(as_text=True))
        self.assertIn('flash-warning', content)
        self.assertIn(message, content)

    def test_login_required_and_user_scoped_for_jobs_and_staged_work(self):
        self.assertEqual(self.client.get("/job-queue").status_code, 302)
        self.db.save_book(Book(abs_id="alice-id", abs_title="Visible Alice",
                               status="processing", user_id=self.alice.id))
        self.db.save_book(Book(abs_id="bob-id", abs_title="Private Bob",
                               status="processing", user_id=self.bob.id))
        self.db.save_job(Job(abs_id="alice-id", last_attempt=time.time(), progress=.4))
        self.db.save_job(Job(abs_id="bob-id", last_attempt=time.time(), progress=.6))
        staged = [
            {"abs_id": "alice-stage", "abs_title": "Alice Staged",
             "user_id": self.alice.id, "readalong_epub_requested": True},
            {"abs_id": "bob-stage", "abs_title": "Bob Staged",
             "user_id": self.bob.id},
        ]
        (Path(self.tmp) / "match_queue.json").write_text(json.dumps(staged))
        self.client.post("/login", data={"username": "alice", "password": "pw-alice"})
        with patch("src.web_server._match_queue_file_path", return_value=Path(self.tmp) / "match_queue.json"):
            page = self.client.get("/job-queue")
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn("Visible Alice", html)
        self.assertIn("Alice Staged", html)
        self.assertIn("EPUB3", html)
        self.assertNotIn("Private Bob", html)
        self.assertNotIn("Bob Staged", html)

    def test_preflight_ui_is_scoped_and_ack_is_only_for_current_pair(self):
        from src.services.transcription_preflight import (
            evidence_identity, pair_key, evaluate_preflight,
        )
        a = Book(abs_id="alice-pair", abs_title="Visible pair",
                 audio_source="ABS", audio_source_id="alice-pair",
                 ebook_source="BookOrbit", ebook_source_id="82",
                 ebook_filename="trim.epub", audio_duration=3600,
                 status="processing", user_id=self.alice.id)
        b = Book(abs_id="bob-pair", abs_title="Private pairing",
                 audio_source="ABS", audio_source_id="bob-pair",
                 ebook_filename="private.epub", audio_duration=3600,
                 status="processing", user_id=self.bob.id)
        self.db.save_book(a)
        self.db.save_book(b)
        self.db.save_job(Job(abs_id=a.abs_id, last_attempt=time.time(), progress=.4))
        self.db.save_job(Job(abs_id=b.abs_id, last_attempt=time.time(), progress=.6))
        evidence_a = evaluate_preflight("synthetic words only", 3600)
        evidence_b = evaluate_preflight("private text", 3600)
        identity = evidence_identity(a, "synthetic words only")
        self.assertTrue(self.db.save_transcription_preflight(
            a.abs_id, self.alice.id, identity, pair_key(a), evidence_a))
        self.assertTrue(self.db.save_transcription_preflight(
            b.abs_id, self.bob.id, evidence_identity(b, "private text"),
            pair_key(b), evidence_b))
        self.client.post("/login", data={"username": "alice", "password": "pw-alice"})
        for path in ("/job-queue",):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            html_page = response.get_data(as_text=True)
            self.assertIn("EPUB/audio pre-flight", html_page)
            self.assertIn(">Dismiss</button>", html_page)
            self.assertIn("EPUB tokens per audio hour", html_page)
            self.assertIn("not a mismatch verdict", html_page)
            self.assertNotIn("Private pairing", html_page)
            self.assertNotIn("private text", html_page)
        self.assertEqual(self.client.post(
            "/job-queue/preflight/ack",
            data={"abs_id": b.abs_id,
                  "identity": evidence_identity(b, "private text")},
        ).status_code, 404)
        self.assertEqual(self.client.post(
            "/job-queue/preflight/ack",
            data={"abs_id": a.abs_id, "identity": "incorrect"},
        ).status_code, 409)
        ok = self.client.post(
            "/job-queue/preflight/ack",
            data={"abs_id": a.abs_id, "identity": identity},
        )
        self.assertEqual(ok.status_code, 302)
        self.assertTrue(
            self.db.get_transcription_preflights(self.alice.id, [a.abs_id])[a.abs_id]["acknowledged"]
        )
        page = self.client.get("/job-queue").get_data(as_text=True)
        self.assertIn("Advisory acknowledged", page)
        self.assertNotIn(">Dismiss</button>", page)
        self.assertIsNone(self.db.get_latest_job(a.abs_id).last_error)
        self.assertEqual(self.db.get_latest_job(a.abs_id).progress, .4)
        self.db.set_user_credential(self.alice.id, "UI_MODE", "new")
        new_page = self.client.get("/job-queue")
        self.assertEqual(new_page.status_code, 200)
        new_html = new_page.get_data(as_text=True)
        self.assertIn("EPUB/audio pre-flight", new_html)
        self.assertIn("Advisory acknowledged", new_html)
        a.ebook_source_id = "different-edition"
        self.db.save_book(a)
        self.assertNotIn("EPUB/audio pre-flight", self.client.get("/job-queue").get_data(as_text=True))
        self.assertEqual(self.client.post(
            "/job-queue/preflight/ack",
            data={"abs_id": a.abs_id, "identity": identity},
        ).status_code, 409)

    def test_inconclusive_preflight_renders_without_rate_and_never_stops_job(self):
        from src.services.transcription_preflight import (
            evaluate_preflight, evidence_identity, pair_key,
        )
        book = Book(abs_id="unspaced528", abs_title="Unspaced script",
                    audio_source="ABS", audio_source_id="unspaced528",
                    ebook_filename="text.epub", duration=3600,
                    status="processing", user_id=self.alice.id)
        self.db.save_book(book)
        self.db.save_job(Job(abs_id=book.abs_id, last_attempt=time.time(), progress=.2))
        text = "你好世界" * 20
        evidence = evaluate_preflight(text, 3600)
        self.assertEqual(evidence["assessment"], "inconclusive")
        self.assertIsNone(evidence["epub_tokens_per_audio_hour"])
        self.db.save_transcription_preflight(
            book.abs_id, self.alice.id,
            evidence_identity(book, text), pair_key(book), evidence,
        )
        self.client.post("/login", data={"username": "alice", "password": "pw-alice"})
        for interface in ("classic", "new"):
            self.db.set_user_credential(self.alice.id, "UI_MODE", interface)
            page = self.client.get("/job-queue")
            self.assertEqual(page.status_code, 200)
            markup = page.get_data(as_text=True)
            self.assertIn("Unspaced script", markup)
            self.assertIn("inconclusive", markup.lower())
            self.assertIn(">Dismiss</button>", markup)
            self.assertNotIn("EPUB tokens per audio hour</span>", markup)

    def test_library_comparison_shown_with_sufficient_baseline_and_hidden_with_fewer(self):
        from src.services.transcription_preflight import evidence_identity, pair_key
        rates = [7000, 7200, 7400, 7600, 8000]
        for i, rate in enumerate(rates, 1):
            abs_id = f"lib{i}"
            book = Book(
                abs_id=abs_id,
                abs_title=f"Active {i}",
                status="active",
                user_id=self.alice.id,
                audio_source="ABS",
                audio_source_id=abs_id,
                ebook_filename="book.epub",
                audio_duration=3600,
            )
            self.db.save_book(book)
            evidence = {
                "assessment": "uncalibrated",
                "token_count": rate,
                "duration_seconds": 3600.0,
                "epub_tokens_per_audio_hour": rate,
            }
            identity = evidence_identity(book, "dummy")
            self.db.save_transcription_preflight(abs_id, self.alice.id, identity, pair_key(book), evidence)

        proc_abs_id = "proc"
        proc_book = Book(
            abs_id=proc_abs_id,
            abs_title="Processing Book",
            status="processing",
            user_id=self.alice.id,
            audio_source="ABS",
            audio_source_id=proc_abs_id,
            ebook_filename="book.epub",
            audio_duration=3600,
        )
        self.db.save_book(proc_book)
        self.db.save_job(Job(abs_id=proc_abs_id, last_attempt=time.time(), progress=0.4))
        evidence_proc = {
            "assessment": "uncalibrated",
            "token_count": 14900,
            "duration_seconds": 3600.0,
            "epub_tokens_per_audio_hour": 14900,
        }
        identity_proc = evidence_identity(proc_book, "dummy")
        self.db.save_transcription_preflight(proc_abs_id, self.alice.id, identity_proc, pair_key(proc_book), evidence_proc)

        self.client.post("/login", data={"username": "alice", "password": "pw-alice"})
        for interface in ("classic", "new"):
            self.db.set_user_credential(self.alice.id, "UI_MODE", interface)
            page = self.client.get("/job-queue")
            self.assertEqual(page.status_code, 200)
            markup = html.unescape(page.get_data(as_text=True))
            self.assertIn("Processing Book", markup)
            self.assertIn("Not calibrated", markup)
            self.assertIn("2.01× your library median", markup)
            self.assertIn("7,000–8,000 across 5 books", markup)

        # Reduce baseline below threshold
        book_to_change = self.db.get_book("lib1")
        book_to_change.status = "pending"
        self.db.save_book(book_to_change)

        self.db.set_user_credential(self.alice.id, "UI_MODE", "classic")
        page = self.client.get("/job-queue")
        markup = html.unescape(page.get_data(as_text=True))
        self.assertNotIn("your library median", markup)

    def test_running_job_queue_page_enables_auto_refresh(self):
        import src.web_server as ws
        with ws._VISIBLE_BATCH_LOCK:
            ws._VISIBLE_BATCHES["auto-refresh"] = {
                "user_id": self.alice.id,
                "label": "Match batch",
                "items": [{"abs_title": "Processing book"}],
                "current": 0,
            }
        self.client.post("/login", data={"username": "alice", "password": "pw-alice"})
        response = self.client.get("/job-queue")
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn('data-auto-refresh="20"', page)
        self.assertIn("Updates every 20 seconds while books are processing.", page)
        self.assertIn("window.setInterval", page)
        self.assertIn("20000", page)
        self.assertIn("document.hidden", page)
        self.assertIn("reorderSubmitting", page)
        self.assertIn("window.location.reload()", page)

    def test_waiting_only_job_queue_page_has_no_auto_refresh(self):
        self.db.save_book(Book(
            abs_id="pending-only", abs_title="Waiting book",
            status="pending", user_id=self.alice.id,
        ))
        self.client.post("/login", data={"username": "alice", "password": "pw-alice"})
        response = self.client.get("/job-queue")
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("Waiting book", page)
        self.assertNotIn('data-auto-refresh="20"', page)
        self.assertNotIn("Updates every 20 seconds while books are processing.", page)
        self.assertNotIn("window.setInterval", page)
        self.assertNotIn("window.location.reload()", page)

    def test_in_process_batch_is_visible_and_cleaned_up(self):
        import src.web_server as ws
        seen = []
        with self.app.test_request_context("/job-queue"):
            def consume(items):
                for item in items:
                    with ws._VISIBLE_BATCH_LOCK:
                        batch = next(iter(ws._VISIBLE_BATCHES.values()))
                        seen.append((batch["current"], batch["items"][0]["abs_title"]))
            with patch.object(ws, "_BACKGROUND_TASKS_SYNCHRONOUS", True):
                ws._spawn_user_background(
                    consume, [{"abs_title": "Safe batch title",
                               "ebook_source_path": "SECRET_PATH"},
                              {"abs_title": "Next"}], label="batch-match-process")
        self.assertEqual(seen, [(0, "Safe batch title"), (1, "Safe batch title")])
        self.assertFalse(ws._VISIBLE_BATCHES)

    def test_active_batch_is_private_to_its_owner(self):
        import src.web_server as ws
        with ws._VISIBLE_BATCH_LOCK:
            ws._VISIBLE_BATCHES["alice"] = {
                "user_id": self.alice.id, "label": "Match batch",
                "items": [{"abs_title": "Alice batch"}], "current": 0,
            }
            ws._VISIBLE_BATCHES["bob"] = {
                "user_id": self.bob.id, "label": "Match batch",
                "items": [{"abs_title": "Private batch"}], "current": 0,
            }
        self.client.post("/login", data={"username": "alice", "password": "pw-alice"})
        page = self.client.get("/job-queue").get_data(as_text=True)
        self.assertIn("Alice batch", page)
        self.assertNotIn("Private batch", page)

    def test_reorder_persists_and_is_used_when_submitting_add_book_queue(self):
        import src.web_server as ws
        path = Path(self.tmp) / "match_queue.json"
        items = [
            {"bridge_key": "a", "abs_id": "a", "abs_title": "Alice First",
             "user_id": self.alice.id},
            {"bridge_key": "b", "abs_id": "b", "abs_title": "Bob Item",
             "user_id": self.bob.id},
            {"bridge_key": "c", "abs_id": "c", "abs_title": "Alice Second",
             "user_id": self.alice.id},
            {"bridge_key": "d", "abs_id": "d", "abs_title": "Alice Third",
             "user_id": self.alice.id},
        ]
        path.write_text(json.dumps({"items": items}), encoding="utf-8")
        self.client.post("/login", data={"username": "alice", "password": "pw-alice"})
        with patch.object(ws, "_match_queue_file_path", return_value=path):
            response = self.client.post("/job-queue/reorder",
                                        data={"queue_key": "d", "direction": "top"})
            self.assertEqual(response.status_code, 302)
            page = self.client.get("/job-queue").get_data(as_text=True)
            self.assertIn("Move Alice Third down", page)
            self.assertNotIn("Move Bob Item up", page)
            token = set_current_user_id(self.alice.id)
            try:
                queued = ws._load_match_queue()
            finally:
                reset_current_user_id(token)
            self.assertEqual([x["bridge_key"] for x in queued], ["d", "a", "c"])
            raw = json.loads(path.read_text(encoding="utf-8"))["items"]
            self.assertEqual([x["bridge_key"] for x in raw], ["d", "b", "a", "c"])
            token = set_current_user_id(self.alice.id)
            try:
                self.assertEqual([x["bridge_key"] for x in ws._match_queue_drain()],
                                 ["d", "a", "c"])
                self.assertEqual([x["bridge_key"] for x in ws._load_match_queue()], [])
            finally:
                reset_current_user_id(token)
            self.assertEqual([x["bridge_key"] for x in
                             json.loads(path.read_text(encoding="utf-8"))["items"]],
                             ["b"])

    def test_reorder_only_applies_to_owned_staged_items(self):
        import src.web_server as ws
        path = Path(self.tmp) / "match_queue.json"
        items = [
            {"bridge_key": "alice", "abs_id": "alice", "user_id": self.alice.id},
            {"bridge_key": "bob", "abs_id": "bob", "user_id": self.bob.id},
        ]
        path.write_text(json.dumps({"items": items}), encoding="utf-8")
        self.assertEqual(self.client.post("/job-queue/reorder",
                          data={"queue_key": "alice", "direction": "top"}).status_code, 302)
        self.client.post("/login", data={"username": "alice", "password": "pw-alice"})
        with patch.object(ws, "_match_queue_file_path", return_value=path):
            self.assertEqual(self.client.get("/job-queue/reorder").status_code, 405)
            self._assert_reorder_warning(
                {"queue_key": "bob", "direction": "top"},
                "That book is no longer waiting in Add Book.",
            )
            self._assert_reorder_warning(
                {"queue_key": "alice", "direction": "diagonal"},
                "That move isn't valid.",
            )
            self.assertEqual(self.client.post("/job-queue/reorder",
                             data={"queue_key": "alice", "direction": "top"}).status_code, 302)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["items"], items)
            token = set_current_user_id(self.alice.id)
            try:
                ws._match_queue_drain()
            finally:
                reset_current_user_id(token)
            self._assert_reorder_warning(
                {"queue_key": "alice", "direction": "up"},
                "That book is no longer waiting in Add Book.",
            )

    def test_reorder_rejects_ambiguous_staged_keys(self):
        import src.web_server as ws
        path = Path(self.tmp) / "match_queue.json"
        items = [
            {"bridge_key": "same", "user_id": self.alice.id},
            {"bridge_key": "same", "user_id": self.alice.id},
        ]
        path.write_text(json.dumps({"items": items}), encoding="utf-8")
        self.client.post("/login", data={"username": "alice", "password": "pw-alice"})
        with patch.object(ws, "_match_queue_file_path", return_value=path):
            self._assert_reorder_warning(
                {"queue_key": "same", "direction": "up"},
                "That book couldn't be identified; refresh and try again.",
            )
            page = self.client.get("/job-queue").get_data(as_text=True)
            self.assertNotIn('name="queue_key"', page)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["items"], items)

    def test_alignment_order_and_owner_slot_moves_persist(self):
        books = [
            ("a", self.alice.id), ("b", self.bob.id),
            ("c", self.alice.id), ("d", self.bob.id),
            ("e", self.alice.id),
        ]
        for abs_id, owner in books:
            self.db.save_book(Book(abs_id=abs_id, abs_title=abs_id,
                                   status="pending", user_id=owner))
        self.assertEqual([book.abs_id for book in self.db.get_pending_alignment_queue()],
                         ["a", "b", "c", "d", "e"])
        self.assertEqual(
            self.db.move_pending_alignment_book(self.alice.id, "e", "top"), "moved")
        ordered = self.db.get_pending_alignment_queue()
        self.assertEqual([book.abs_id for book in ordered],
                         ["e", "b", "a", "d", "c"])
        self.assertEqual([book.alignment_queue_position for book in ordered],
                         [1., 2., 3., 4., 5.])
        self.assertEqual(self.db.move_pending_alignment_book(
            self.alice.id, "e", "up"), "unchanged")
        self.assertEqual(self.db.move_pending_alignment_book(
            self.alice.id, "a", "down"), "moved")
        self.assertEqual([book.abs_id for book in self.db.get_pending_alignment_queue()],
                         ["e", "b", "c", "d", "a"])
        self.assertEqual(self.db.move_pending_alignment_book(
            self.alice.id, "b", "top"), "missing")
        self.assertEqual(self.db.move_pending_alignment_book(
            self.bob.id, "a", "top"), "missing")
        self.assertEqual(self.db.move_pending_alignment_book(
            self.alice.id, "missing", "up"), "missing")
        self.assertEqual(self.db.move_pending_alignment_book(
            self.alice.id, "c", "invalid"), "missing")
        self.assertEqual([book.abs_id for book in self.db.get_pending_alignment_queue()],
                         ["e", "b", "c", "d", "a"])

    def test_alignment_nulls_follow_positioned_and_selected_cleared(self):
        self.db.save_book(Book(abs_id="a", abs_title="A", status="pending",
                               user_id=self.alice.id))
        self.db.save_book(Book(abs_id="b", abs_title="B", status="pending",
                               user_id=self.alice.id, alignment_queue_position=0.5))
        self.db.save_book(Book(abs_id="c", abs_title="C", status="pending",
                               user_id=self.alice.id))
        self.assertEqual([b.abs_id for b in self.db.get_pending_alignment_queue()],
                         ["b", "a", "c"])
        self.db.clear_alignment_queue_position("b")
        self.assertIsNone(self.db.get_book("b").alignment_queue_position)
        self.assertEqual([b.abs_id for b in self.db.get_pending_alignment_queue()],
                         ["a", "b", "c"])
        self.assertEqual(self.db.move_pending_alignment_book(
            self.alice.id, "c", "top"), "moved")
        # A newly pending legacy book joins the end, without any move request.
        self.db.save_book(Book(abs_id="new", abs_title="New", status="pending",
                               user_id=self.alice.id))
        self.assertEqual([b.abs_id for b in self.db.get_pending_alignment_queue()],
                         ["c", "a", "b", "new"])

    def test_alignment_route_visibility_reorder_and_stale_warnings(self):
        self.db.save_book(Book(abs_id="a", abs_title="Alice First", status="pending",
                               user_id=self.alice.id))
        self.db.save_book(Book(abs_id="b", abs_title="Bob Private", status="pending",
                               user_id=self.bob.id))
        self.db.save_book(Book(abs_id="c", abs_title="Alice Second", status="pending",
                               user_id=self.alice.id))
        self.client.post("/login", data={"username": "alice", "password": "pw-alice"})
        page = self.client.get("/job-queue").get_data(as_text=True)
        self.assertIn("#3 of 3 waiting for alignment", page)
        self.assertIn('name="queue" value="alignment"', page)
        self.assertNotIn("Bob Private", page)
        response = self.client.post(
            "/job-queue/reorder", data={
                "queue": "alignment", "queue_key": "c", "direction": "top",
            })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/job-queue")
        self.assertEqual([b.abs_id for b in self.db.get_pending_alignment_queue()],
                         ["c", "b", "a"])
        self._assert_reorder_warning(
            {"queue": "alignment", "queue_key": "b", "direction": "up"},
            "That book is no longer waiting for alignment.",
        )
        self._assert_reorder_warning(
            {"queue": "alignment", "queue_key": "c", "direction": "sideways"},
            "That move isn't valid.",
        )
        self.db.save_book(Book(abs_id="c", abs_title="Alice Second", status="processing",
                               user_id=self.alice.id))
        self._assert_reorder_warning(
            {"queue": "alignment", "queue_key": "c", "direction": "down"},
            "That book is no longer waiting for alignment.",
        )
        self.assertEqual([b.abs_id for b in self.db.get_pending_alignment_queue()],
                         ["b", "a"])

    def test_alignment_reorder_requires_owner_even_if_other_user_claimed_book(self):
        self.db.save_book(Book(abs_id="owned", abs_title="Owned by Alice",
                               status="pending", user_id=self.alice.id))
        self.db.save_book(Book(abs_id="bob", abs_title="Owned by Bob",
                               status="pending", user_id=self.bob.id))
        self.db.link_user_book(self.bob.id, "owned")
        self.assertEqual(
            self.db.move_pending_alignment_book(self.bob.id, "owned", "top"),
            "missing",
        )
        self.client.post("/login", data={"username": "bob", "password": "pw-bob"})
        page = self.client.get("/job-queue").get_data(as_text=True)
        self.assertIn("Owned by Alice", page)
        # Only Bob's own pending book has reorder controls.
        self.assertNotIn('value="owned"', page)
        self.assertIn('value="bob"', page)

    def test_failed_batch_does_not_leave_a_phantom_queue(self):
        import src.web_server as ws
        with self.app.test_request_context("/job-queue"):
            def fail(items):
                next(iter(items))
                raise RuntimeError("worker broke")
            with patch.object(ws, "_BACKGROUND_TASKS_SYNCHRONOUS", True):
                ws._spawn_user_background(
                    fail, [{"abs_title": "Broken job"}], label="batch-match-process",
                )
        self.assertFalse(ws._VISIBLE_BATCHES)
