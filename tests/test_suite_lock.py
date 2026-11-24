import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import conftest
from suite_lock import SuiteLock

ROOT = Path(__file__).resolve().parent.parent
TESTS = Path(__file__).resolve().parent


def _try_lock(path, close_on_exit=True):
    lines = [
        "import sys",
        "sys.path.insert(0, sys.argv[1])",
        "from suite_lock import SuiteLock, SuiteLockBusy",
        "lock = SuiteLock(sys.argv[2])",
        "try:",
        "    lock.acquire()",
        "except SuiteLockBusy:",
        "    sys.stdout.write('busy\\n')",
        "    sys.stdout.flush()",
        "    raise SystemExit(2)",
        "sys.stdout.write('acquired\\n')",
        "sys.stdout.flush()",
    ]
    if close_on_exit:
        lines.append("lock.close()")
    code = "\n".join(lines)
    return subprocess.run(
        [sys.executable, "-c", code, str(TESTS), str(path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )


def test_file_lock_is_exclusive_across_processes_and_releases_on_close(tmp_path):
    path = tmp_path / "suite.lock"
    owner = SuiteLock(path)
    owner.acquire()
    try:
        contender = _try_lock(path)
        assert contender.returncode == 2
        assert contender.stdout.strip() == "busy"
    finally:
        owner.close()

    reacquired = _try_lock(path)
    assert reacquired.returncode == 0
    assert reacquired.stdout.strip() == "acquired"


def test_file_lock_releases_when_owner_process_exits(tmp_path):
    path = tmp_path / "suite.lock"
    acquired = _try_lock(path, close_on_exit=False)
    assert acquired.returncode == 0

    owner = SuiteLock(path)
    owner.acquire()
    owner.close()
    assert _try_lock(path).returncode == 0


def test_file_lock_releases_when_owner_process_is_killed(tmp_path):
    path = tmp_path / "suite.lock"
    ready = tmp_path / "owner-ready"
    code = "\n".join(
        (
            "import sys, time",
            "sys.path.insert(0, sys.argv[1])",
            "from suite_lock import SuiteLock",
            "lock = SuiteLock(sys.argv[2])",
            "lock.acquire()",
            "open(sys.argv[3], 'w').close()",
            "time.sleep(60)",
        )
    )
    owner = subprocess.Popen(
        [sys.executable, "-c", code, str(TESTS), str(path), str(ready)],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    acquired = False
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and owner.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        acquired = ready.exists()
    finally:
        if owner.poll() is None:
            owner.kill()
        owner.wait(timeout=5)
        owner.stdout.close()
        owner.stderr.close()

    assert acquired
    assert owner.returncode != 0
    assert _try_lock(path).returncode == 0


def test_session_start_skips_workers_and_reports_busy_controller(monkeypatch, tmp_path):
    path = tmp_path / "suite.lock"
    monkeypatch.setattr(conftest, "TEST_SUITE_LOCK_PATH", path)
    worker = SimpleNamespace(config=SimpleNamespace(workerinput={}))
    conftest.pytest_sessionstart(worker)
    assert not path.exists()

    owner = SuiteLock(path)
    owner.acquire()
    cleanups = []
    controller = SimpleNamespace(config=SimpleNamespace(add_cleanup=cleanups.append))
    try:
        with pytest.raises(pytest.UsageError, match="Another BookBridge pytest controller is already running") as exc:
            conftest.pytest_sessionstart(controller)
        assert "Wait for it to finish" in str(exc.value)
    finally:
        for cleanup in cleanups:
            cleanup()
        owner.close()


def test_collect_only_rejects_busy_controller_before_importing_tests(tmp_path):
    sentinel = tmp_path / "test_collection_sentinel.py"
    sentinel.write_text('raise RuntimeError("COLLECTION_SENTINEL_IMPORTED")\n')
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        value for value in (str(TESTS), env.get("PYTHONPATH", "")) if value
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-p",
            "conftest",
            str(sentinel),
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 4
    assert "Another BookBridge pytest controller is already running" in output
    assert "COLLECTION_SENTINEL_IMPORTED" not in output
