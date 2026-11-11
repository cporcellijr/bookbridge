"""One Suggestions scan at a time across readers; the rest wait as 'queued'."""

import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import src.web_server as web_server
from src.utils.user_context import get_current_user_id, reset_current_user_id, set_current_user_id


def _wait_for(condition, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


class ScanQueueTests(unittest.TestCase):
    def setUp(self):
        self.gates = {}
        self.ran_for = []
        self.fail_for = set()
        for patcher in (
            patch.dict(web_server.SUGGESTIONS_SCAN_JOBS, {}, clear=True),
            patch.object(web_server, "_BACKGROUND_TASKS_SYNCHRONOUS", False),
            patch.object(web_server, "_run_suggestions_scan_job", side_effect=self._fake_run),
            patch.object(web_server, "uc", return_value=Mock()),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self._release_all)

    def _release_all(self):
        for gate in self.gates.values():
            gate.set()
        _wait_for(lambda: not web_server.SUGGESTIONS_SCAN_SLOT.locked())

    def _fake_run(self, job_id, *_args):
        user_id = get_current_user_id()
        self.ran_for.append(user_id)
        self.gates.setdefault(user_id, threading.Event()).wait(5)
        if user_id in self.fail_for:
            raise RuntimeError("scan blew up")
        with web_server.SUGGESTIONS_SCAN_JOBS_LOCK:
            web_server.SUGGESTIONS_SCAN_JOBS[job_id]["status"] = "done"

    def _start(self, user_id):
        self.gates.setdefault(user_id, threading.Event())
        token = set_current_user_id(user_id)
        try:
            return web_server._start_suggestions_scan_job({}, [])
        finally:
            reset_current_user_id(token)

    def _status(self, job_id):
        return (web_server._get_suggestions_scan_job(job_id) or {}).get("status")

    def test_second_reader_scan_waits_queued_until_the_first_finishes(self):
        first = self._start(1)
        self.assertTrue(_wait_for(lambda: self._status(first) == "running"))
        second = self._start(2)
        time.sleep(0.1)
        self.assertEqual("queued", self._status(second))
        self.assertEqual([1], self.ran_for)

        self.gates[1].set()
        self.assertTrue(_wait_for(lambda: self._status(second) == "running"))
        self.assertEqual("done", self._status(first))
        self.gates[2].set()
        self.assertTrue(_wait_for(lambda: self._status(second) == "done"))
        self.assertEqual([1, 2], self.ran_for)

    def test_same_reader_request_reuses_its_queued_or_running_job(self):
        first = self._start(1)
        self.assertTrue(_wait_for(lambda: self._status(first) == "running"))
        self.assertEqual(first, self._start(1))
        queued = self._start(2)
        self.assertEqual(queued, self._start(2))
        self.assertEqual(2, len(web_server.SUGGESTIONS_SCAN_JOBS))

    def test_failed_scan_releases_the_slot(self):
        self.fail_for.add(1)
        with patch.object(threading, "excepthook", lambda _args: None):
            first = self._start(1)
            self.assertTrue(_wait_for(lambda: self.ran_for == [1]))
            second = self._start(2)
            self.gates[1].set()
            self.assertTrue(_wait_for(lambda: self._status(second) == "running"))
        self.assertEqual("running", self._status(first))

    def test_queued_jobs_are_never_pruned_while_they_wait(self):
        web_server.SUGGESTIONS_SCAN_JOBS["old-queued"] = {"status": "queued", "user_id": 2, "updated_at": 0}
        web_server.SUGGESTIONS_SCAN_JOBS["old-done"] = {"status": "done", "user_id": 1, "updated_at": 0}
        web_server._prune_suggestions_scan_jobs()
        self.assertIn("old-queued", web_server.SUGGESTIONS_SCAN_JOBS)
        self.assertNotIn("old-done", web_server.SUGGESTIONS_SCAN_JOBS)


class ScanJobOwnershipTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict(web_server.SUGGESTIONS_SCAN_JOBS, {
            "alice-job": {"status": "done", "user_id": 1, "results": {"suggestions": ["alice"]}},
        }, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _as(self, user_id):
        token = set_current_user_id(user_id)
        self.addCleanup(reset_current_user_id, token)

    def test_another_readers_job_id_is_not_readable(self):
        self._as(2)
        self.assertIsNone(web_server._owned_suggestions_scan_job("alice-job"))

    def test_the_owner_reads_their_job(self):
        self._as(1)
        self.assertEqual(["alice"], web_server._owned_suggestions_scan_job("alice-job")["results"]["suggestions"])


if __name__ == "__main__":
    unittest.main()
