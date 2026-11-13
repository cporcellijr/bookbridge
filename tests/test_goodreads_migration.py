"""Goodreads schema and database methods on a fresh migrated SQLite file."""

import tempfile
import unittest
from pathlib import Path

import sqlalchemy as sa

from src.db.database_service import DatabaseService
from src.db.models import Book, GoodreadsDetails


class TestGoodreadsMigration(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = DatabaseService(str(Path(self.temp.name) / "goodreads.db"))

    def tearDown(self) -> None:
        self.db.db_manager.close()
        self.temp.cleanup()

    def test_base_to_head_has_goodreads_table(self) -> None:
        inspector = sa.inspect(self.db.db_manager.engine)
        columns = {column["name"] for column in inspector.get_columns("goodreads_details")}
        self.assertIn("goodreads_book_id", columns)
        self.assertIn("goodreads_rating_updated_at", columns)
        self.assertIn("created_at", columns)
        self.assertIn("updated_at", columns)

    def test_details_crud_round_trip(self) -> None:
        self.db.save_book(Book(abs_id="a1", abs_title="Example", status="active"))
        first = GoodreadsDetails("a1", "123", goodreads_pages=320, matched_by="grimmory_id")
        self.db.save_goodreads_details(first)
        self.assertEqual(self.db.get_goodreads_details("a1").goodreads_book_id, "123")
        self.db.save_goodreads_details(GoodreadsDetails("a1", "456", goodreads_rating=4.2))
        linked = self.db.get_goodreads_details("a1")
        self.assertEqual((linked.goodreads_book_id, linked.goodreads_pages), ("456", 320))
        self.assertEqual(len(self.db.get_all_goodreads_details()), 1)
        self.assertTrue(self.db.delete_goodreads_details("a1"))
        self.assertIsNone(self.db.get_goodreads_details("a1"))


    def test_migration_creates_the_table_only_when_missing(self) -> None:
        """Preview-branch installs already have the table; every other install gets it."""
        import importlib.util

        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "e3b7c1a9d2f4_add_goodreads_details.py"
        spec = importlib.util.spec_from_file_location("goodreads_details_migration", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = self.db.db_manager.engine
        self.db.save_book(Book(abs_id="a1", abs_title="Example", status="active"))
        self.db.save_goodreads_details(GoodreadsDetails("a1", "123"))

        def upgrade() -> None:
            with engine.begin() as conn:
                with Operations.context(MigrationContext.configure(conn)):
                    migration.upgrade()

        upgrade()  # Table already present: kept, rows untouched.
        self.assertEqual(self.db.get_goodreads_details("a1").goodreads_book_id, "123")

        with engine.begin() as conn:
            conn.execute(sa.text("DROP TABLE goodreads_details"))
        upgrade()  # Table missing: created.
        self.assertIn("goodreads_details", sa.inspect(engine).get_table_names())


if __name__ == "__main__":
    unittest.main()
