"""Issue #426 phase 4: the `book_alignments.quality_score` / `quality_detail`
migration applies base -> head on a fresh temp SQLite, additively.

Mirrors the `DatabaseService(tmp_path)`-based migration testing already used in
`tests/test_database_service_integration.py` and `tests/test_map_publish_seam.py`:
constructing `DatabaseService` against a brand-new temp file runs Alembic's full
`command.upgrade(cfg, "head")` chain from base, which is the safe way to exercise
a migration in this repo (see CLAUDE.md's alembic warning about the real `/data`
bind mount).
"""

import unittest

import sqlalchemy as sa

from src.db.database_service import DatabaseService


class TestAlignmentQualityMigrationAppliesToHead(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path

        self.temp_dir = tempfile.mkdtemp()
        self.db_path = str(Path(self.temp_dir) / "quality_migration.db")
        self.db_service = DatabaseService(self.db_path)

    def tearDown(self):
        self.db_service.db_manager.close()
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_both_columns_exist_and_are_nullable(self):
        inspector = sa.inspect(self.db_service.db_manager.engine)
        columns = {c["name"]: c for c in inspector.get_columns("book_alignments")}

        self.assertIn("quality_score", columns)
        self.assertIn("quality_detail", columns)
        self.assertTrue(columns["quality_score"]["nullable"])
        self.assertTrue(columns["quality_detail"]["nullable"])

    def test_orm_model_matches_the_migrated_schema(self):
        """A model that drifts from the migration is exactly the class of bug
        this test catches: a freshly created DB (Base.metadata.create_all) and a
        migrated one would silently disagree on column shape."""
        from src.db.models import Base

        migrated = sa.inspect(self.db_service.db_manager.engine)
        mig_cols = {
            c["name"]: str(c["type"])
            for c in migrated.get_columns("book_alignments")
        }

        model_engine = sa.create_engine("sqlite:///:memory:")
        try:
            Base.metadata.create_all(model_engine)
            built = sa.inspect(model_engine)
            built_cols = {
                c["name"]: str(c["type"])
                for c in built.get_columns("book_alignments")
            }
        finally:
            model_engine.dispose()

        self.assertEqual(mig_cols.get("quality_score"), built_cols.get("quality_score"))
        self.assertEqual(mig_cols.get("quality_detail"), built_cols.get("quality_detail"))


class TestAlignmentQualityMigrationModuleDirectly(unittest.TestCase):
    """Exercises the migration's own upgrade/downgrade against a pre-existing
    `book_alignments` table, isolated from the rest of the chain (mirrors
    `tests/test_kosync_canonical_migration.py`'s direct-module pattern)."""

    def setUp(self):
        import importlib.util
        from pathlib import Path

        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        migration_path = (
            Path(__file__).resolve().parents[1]
            / "alembic/versions/8e5b7a15f647_add_alignment_quality_score.py"
        )
        spec = importlib.util.spec_from_file_location("quality_migration", migration_path)
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)

        self.engine = sa.create_engine("sqlite:///:memory:")
        with self.engine.begin() as conn:
            conn.execute(sa.text(
                "CREATE TABLE book_alignments ("
                "abs_id VARCHAR(255) PRIMARY KEY, "
                "alignment_map_json TEXT NOT NULL, "
                "align_method VARCHAR(32), "
                "total_chars INTEGER)"
            ))
        self._MigrationContext = MigrationContext
        self._Operations = Operations

    def tearDown(self):
        self.engine.dispose()

    def _run(self, fn):
        with self.engine.begin() as conn:
            ctx = self._MigrationContext.configure(conn)
            operations = self._Operations(ctx)
            old_op = self.mod.op
            try:
                self.mod.op = operations
                fn()
            finally:
                self.mod.op = old_op

    def _columns(self):
        return {c["name"]: c for c in sa.inspect(self.engine).get_columns("book_alignments")}

    def test_upgrade_adds_both_nullable_columns(self):
        self._run(self.mod.upgrade)
        columns = self._columns()
        self.assertIn("quality_score", columns)
        self.assertIn("quality_detail", columns)
        self.assertTrue(columns["quality_score"]["nullable"])
        self.assertTrue(columns["quality_detail"]["nullable"])

    def test_upgrade_is_idempotent(self):
        self._run(self.mod.upgrade)
        self._run(self.mod.upgrade)
        columns = self._columns()
        self.assertIn("quality_score", columns)
        self.assertIn("quality_detail", columns)

    def test_downgrade_drops_both_columns(self):
        self._run(self.mod.upgrade)
        self._run(self.mod.downgrade)
        columns = self._columns()
        self.assertNotIn("quality_score", columns)
        self.assertNotIn("quality_detail", columns)

    def test_downgrade_is_idempotent(self):
        self._run(self.mod.upgrade)
        self._run(self.mod.downgrade)
        self._run(self.mod.downgrade)
        columns = self._columns()
        self.assertNotIn("quality_score", columns)
        self.assertNotIn("quality_detail", columns)


if __name__ == "__main__":
    unittest.main()
