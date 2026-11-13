import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
TESTS = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

from src.db.database_service import DatabaseService
from suite_lock import SuiteLock, SuiteLockBusy

TEST_SUITE_LOCK_PATH = Path(tempfile.gettempdir()) / "bookbridge-pytest-controller.lock"


def pytest_sessionstart(session: pytest.Session) -> None:
    """Prevent two pytest controllers from running the suite at once."""
    config = session.config
    if hasattr(config, "workerinput"):
        return

    lock = SuiteLock(TEST_SUITE_LOCK_PATH)
    config.add_cleanup(lock.close)
    try:
        lock.acquire()
    except SuiteLockBusy as exc:
        raise pytest.UsageError(
            "Another BookBridge pytest controller is already running "
            f"(lock: {exc}). Wait for it to finish before retrying."
        ) from exc

# Env keys the per-module restore must NOT rewind (see restore_environ_after_module).
_ENVIRON_CARRY_OVER = ("DATA_DIR",)


@pytest.fixture(scope="module", autouse=True)
def restore_environ_after_module():
    """Contain each module's os.environ writes so the suite passes in any order.

    `create_app()` runs `ConfigLoader.load_settings()`, which mirrors every DB
    setting into `os.environ` (DB wins over compose env, by design). Sixteen test
    modules call `create_app()`, and that write used to outlive them and
    reconfigure every later module in the same process — it left
    `BOOKORBIT_ENABLED='false'` with blank credentials, so `is_configured()`
    returned False and `test_bookorbit_client` / `test_booklore_client` failed
    whenever they ran after one of those modules. Only file order hid it.

    Snapshotting at module setup (not import) keeps every module's own
    import-time env setup intact, because pytest imports all modules during
    collection before any test runs. Restoring at module teardown leaves
    behavior *within* a module untouched.
    """
    snapshot = dict(os.environ)
    try:
        yield
    finally:
        # DATA_DIR is test-harness plumbing, not a service setting: modules set it
        # at import to point at their own temp dir, and rewinding it would retarget
        # a DatabaseService/log path that a module is still holding open (on Windows
        # that surfaces as a PermissionError in a test's own rmtree cleanup). Leave
        # it exactly as it behaved before this fixture existed.
        carry_over = {k: os.environ[k] for k in _ENVIRON_CARRY_OVER if k in os.environ}
        os.environ.clear()
        os.environ.update(snapshot)
        os.environ.update(carry_over)


@pytest.fixture(scope="session")
def migrated_database_template(tmp_path_factory):
    """Build the current empty schema once for ordinary isolated DB tests."""
    template = tmp_path_factory.mktemp("database-template") / "empty.db"
    service = DatabaseService(str(template))
    service.db_manager.close()
    return template


@pytest.fixture(autouse=True)
def clone_migrated_database(request, monkeypatch, migrated_database_template):
    """Clone the migrated template instead of rerunning Alembic for every test."""
    if request.node.get_closest_marker("real_database_migrations"):
        yield
        return

    original_init = DatabaseService.__init__

    def fast_init(service, db_path):
        path = Path(db_path).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or path.stat().st_size == 0:
            shutil.copyfile(migrated_database_template, path)
            with patch.object(DatabaseService, "_run_alembic_migrations"):
                original_init(service, str(path))
            return
        original_init(service, str(path))

    monkeypatch.setattr(DatabaseService, "__init__", fast_init)
    yield


@pytest.fixture(autouse=True)
def fast_password_hashing(request, monkeypatch):
    """Use a valid low-cost password hash except in the security contract test."""
    if request.node.get_closest_marker("production_password_hash"):
        yield
        return

    from werkzeug import security

    production_hash = security.generate_password_hash

    def generate_test_hash(password, method="scrypt", salt_length=16):
        return production_hash(password, method="pbkdf2:sha256:1", salt_length=4)

    monkeypatch.setattr(security, "generate_password_hash", generate_test_hash)
    yield
