"""The `calibre_library_files` migration creates its table once and matches the model."""

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from src.db.database_service import DatabaseService
from src.db.models import Base

_MIGRATION = Path(__file__).resolve().parents[1] / "alembic/versions/c4e7a2b9d1f3_add_calibre_library_files.py"


@pytest.fixture
def migration():
    spec = importlib.util.spec_from_file_location("calibre_library_files_migration", _MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def engine():
    engine = sa.create_engine("sqlite:///:memory:")
    yield engine
    engine.dispose()


def _run(engine, migration, fn):
    with engine.begin() as conn:
        old_op = migration.op
        try:
            migration.op = Operations(MigrationContext.configure(conn))
            fn()
        finally:
            migration.op = old_op


def _columns(engine) -> dict:
    return {c["name"]: c for c in sa.inspect(engine).get_columns("calibre_library_files")}


def test_upgrade_twice_creates_the_table_once(engine, migration):
    _run(engine, migration, migration.upgrade)
    _run(engine, migration, migration.upgrade)

    assert set(_columns(engine)) == {"filename", "calibre_book_id", "updated_at"}


def test_downgrade_drops_the_table_and_tolerates_a_missing_one(engine, migration):
    _run(engine, migration, migration.upgrade)
    _run(engine, migration, migration.downgrade)
    _run(engine, migration, migration.downgrade)

    assert "calibre_library_files" not in sa.inspect(engine).get_table_names()


def test_the_migrated_table_matches_the_model(tmp_path):
    service = DatabaseService(str(tmp_path / "calibre_files_migration.db"))
    try:
        migrated = {
            c["name"]: (str(c["type"]), c["nullable"])
            for c in sa.inspect(service.db_manager.engine).get_columns("calibre_library_files")
        }
    finally:
        service.db_manager.close()
    model_engine = sa.create_engine("sqlite:///:memory:")
    try:
        Base.metadata.create_all(model_engine)
        built = {
            c["name"]: (str(c["type"]), c["nullable"])
            for c in sa.inspect(model_engine).get_columns("calibre_library_files")
        }
    finally:
        model_engine.dispose()

    assert migrated == built
