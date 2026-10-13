"""The `kosync_documents.hash_method` migration: applies base -> head additively and
backfills the method of every row that carries a cached filename."""

import hashlib
import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path

import sqlalchemy as sa

from src.db.database_service import DatabaseService

NAME = "Some Book - Some Author.epub"
NAME_HASH = hashlib.md5(NAME.encode("utf-8")).hexdigest()


class TestHashMethodMigrationAppliesToHead(unittest.TestCase):
    def test_fresh_database_has_a_nullable_hash_method_column(self) -> None:
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        db = DatabaseService(str(Path(tmp) / "m.db"))
        self.addCleanup(db.db_manager.close)
        columns = {c["name"]: c for c in sa.inspect(db.db_manager.engine).get_columns("kosync_documents")}
        self.assertIn("hash_method", columns)
        self.assertTrue(columns["hash_method"]["nullable"])


class TestHashMethodMigrationModuleDirectly(unittest.TestCase):
    def setUp(self) -> None:
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        path = Path(__file__).resolve().parents[1] / "alembic/versions/a3d9c5e7b142_add_kosync_document_hash_method.py"
        spec = importlib.util.spec_from_file_location("hash_method_migration", path)
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)
        self.engine = sa.create_engine("sqlite:///:memory:")
        with self.engine.begin() as conn:
            conn.execute(sa.text(
                "CREATE TABLE kosync_documents (document_hash VARCHAR(32) PRIMARY KEY, filename VARCHAR(500))"
            ))
            conn.execute(sa.text("INSERT INTO kosync_documents VALUES (:h, :f)"), {"h": NAME_HASH, "f": NAME})
            conn.execute(sa.text("INSERT INTO kosync_documents VALUES (:h, :f)"), {"h": "a" * 32, "f": NAME})
            conn.execute(sa.text("INSERT INTO kosync_documents VALUES (:h, NULL)"), {"h": "b" * 32})
        self._ctx, self._ops = MigrationContext, Operations
        self.addCleanup(self.engine.dispose)

    def _run(self, fn) -> None:
        with self.engine.begin() as conn:
            old = self.mod.op
            try:
                self.mod.op = self._ops(self._ctx.configure(conn))
                fn()
            finally:
                self.mod.op = old

    def _methods(self) -> dict:
        with self.engine.begin() as conn:
            return dict(conn.execute(sa.text("SELECT document_hash, hash_method FROM kosync_documents")).fetchall())

    def test_upgrade_backfills_by_filename_hash_and_leaves_filenameless_rows_null(self) -> None:
        self._run(self.mod.upgrade)
        self.assertEqual(self._methods(), {NAME_HASH: "filename", "a" * 32: "content", "b" * 32: None})

    def test_upgrade_is_idempotent_and_downgrade_drops_the_column(self) -> None:
        self._run(self.mod.upgrade)
        self._run(self.mod.upgrade)
        self._run(self.mod.downgrade)
        self._run(self.mod.downgrade)
        names = {c["name"] for c in sa.inspect(self.engine).get_columns("kosync_documents")}
        self.assertNotIn("hash_method", names)


if __name__ == "__main__":
    unittest.main()
