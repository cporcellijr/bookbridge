"""Phase 1 Job Queue: visibility, worker handoff, auth and EPUB3 intent."""
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
