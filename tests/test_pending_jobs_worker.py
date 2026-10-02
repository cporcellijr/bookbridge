import threading
import time
from types import SimpleNamespace


def _web_server(monkeypatch, check_pending_jobs):
    import src.web_server as ws

    monkeypatch.setattr(ws, "manager", SimpleNamespace(check_pending_jobs=check_pending_jobs))
    monkeypatch.setattr(ws, "_pending_jobs_thread", None, raising=False)
    return ws


def _wait(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_queue_runs_off_the_calling_thread(monkeypatch):
    ran_on = {}
    started = threading.Event()

    def fake_check():
        ran_on["thread"] = threading.current_thread().name
        started.set()

    ws = _web_server(monkeypatch, fake_check)

    assert ws._check_pending_jobs_async() is True
    assert started.wait(timeout=5)
    ws._pending_jobs_thread.join(timeout=5)

    assert ran_on["thread"] == "pending-jobs"
    assert ran_on["thread"] != threading.current_thread().name


def test_the_scheduler_is_not_blocked_by_a_long_job(monkeypatch):
    release = threading.Event()
    running = threading.Event()

    def slow_check():
        running.set()
        release.wait(timeout=10)

    ws = _web_server(monkeypatch, slow_check)

    try:
        started = time.monotonic()
        assert ws._check_pending_jobs_async() is True
        elapsed = time.monotonic() - started

        assert running.wait(timeout=5)
        assert elapsed < 1.0
    finally:
        release.set()
        if ws._pending_jobs_thread is not None:
            ws._pending_jobs_thread.join(timeout=5)


def test_a_tick_while_the_worker_is_busy_is_a_no_op(monkeypatch):
    release = threading.Event()
    calls = []

    def slow_check():
        calls.append(1)
        release.wait(timeout=10)

    ws = _web_server(monkeypatch, slow_check)

    try:
        assert ws._check_pending_jobs_async() is True
        assert _wait(lambda: calls)
        first = ws._pending_jobs_thread

        assert ws._check_pending_jobs_async() is False
        assert ws._pending_jobs_thread is first
        assert len(calls) == 1
    finally:
        release.set()
        if ws._pending_jobs_thread is not None:
            ws._pending_jobs_thread.join(timeout=5)


def test_a_later_tick_starts_a_fresh_worker(monkeypatch):
    calls = []
    ws = _web_server(monkeypatch, lambda: calls.append(1))

    assert ws._check_pending_jobs_async() is True
    ws._pending_jobs_thread.join(timeout=5)
    assert ws._check_pending_jobs_async() is True
    ws._pending_jobs_thread.join(timeout=5)

    assert _wait(lambda: len(calls) == 2)


def test_a_failing_job_does_not_wedge_the_worker(monkeypatch):
    def boom():
        raise RuntimeError("job exploded")

    ws = _web_server(monkeypatch, boom)

    assert ws._check_pending_jobs_async() is True
    ws._pending_jobs_thread.join(timeout=5)

    ws.manager.check_pending_jobs = lambda: None
    assert ws._check_pending_jobs_async() is True
    ws._pending_jobs_thread.join(timeout=5)
