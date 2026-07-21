import shutil
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.db.database_service import DatabaseService


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
