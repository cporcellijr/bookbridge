"""Alignment position migration: temporary SQLite only, no live database."""
import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from src.db.database_service import DatabaseService
from src.db.models import Base, Book


class TestAlignmentQueueMigration(unittest.TestCase):
    def test_migration_chain_matches_model_on_temporary_database(self):
        with tempfile.TemporaryDirectory() as directory:
            db = DatabaseService(str(Path(directory) / "alignment-migration.db"))
            try:
                engine = db.db_manager.engine
                migrated = {c["name"]: c for c in sa.inspect(engine).get_columns("books")}
                self.assertIn("alignment_queue_position", migrated)
                self.assertTrue(migrated["alignment_queue_position"]["nullable"])
                self.assertIsInstance(migrated["alignment_queue_position"]["type"], sa.Float)
                model_engine = sa.create_engine("sqlite:///:memory:")
                try:
                    Base.metadata.create_all(model_engine)
                    model_columns = {c["name"]: c for c in sa.inspect(model_engine).get_columns("books")}
                    self.assertEqual(str(migrated["alignment_queue_position"]["type"]),
                                     str(model_columns["alignment_queue_position"]["type"]))
                finally:
                    model_engine.dispose()
                db.save_book(Book(abs_id="preexisting", abs_title="Older", status="pending"))
                self.assertIsNone(db.get_book("preexisting").alignment_queue_position)
            finally:
                db.db_manager.close()

    def test_upgrade_preserves_rows_and_downgrade_removes_only_new_column(self):
        source = Path(__file__).resolve().parents[1] / (
            "alembic/versions/a8c7e0d1b529_add_alignment_queue_position.py")
        spec = importlib.util.spec_from_file_location("alignment_queue_migration", source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(module.down_revision, "9b3e5d7f1a42")
        engine = sa.create_engine("sqlite:///:memory:")
        try:
            with engine.begin() as conn:
                conn.execute(sa.text(
                    "CREATE TABLE books (abs_id VARCHAR(255) PRIMARY KEY, status VARCHAR(50))"))
                conn.execute(sa.text("INSERT INTO books (abs_id, status) VALUES ('old', 'pending')"))
            def apply(fn):
                with engine.begin() as conn:
                    original = module.op
                    module.op = Operations(MigrationContext.configure(conn))
                    try:
                        fn()
                    finally:
                        module.op = original
            apply(module.upgrade)
            with engine.connect() as conn:
                self.assertIsNone(conn.execute(sa.text(
                    "SELECT alignment_queue_position FROM books WHERE abs_id='old'")).scalar())
            apply(module.downgrade)
            self.assertNotIn("alignment_queue_position",
                             {c["name"] for c in sa.inspect(engine).get_columns("books")})
            with engine.connect() as conn:
                self.assertEqual(conn.execute(sa.text(
                    "SELECT status FROM books WHERE abs_id='old'")).scalar(), "pending")
        finally:
            engine.dispose()
