"""Migration tests for per-reader synopsis isolation using temporary SQLite."""

import tempfile
import unittest
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

pytestmark = pytest.mark.real_database_migrations
ROOT = Path(__file__).resolve().parents[1]
HEAD = "c4e7a2b9d1f3"
PREVIOUS = "d585c20261112"


class TestUserBookSynopsisMigration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "migration.db"
        self.cfg = Config(str(ROOT / "alembic.ini"))
        self.cfg.set_main_option("script_location", str(ROOT / "alembic"))
        self.cfg.set_main_option("sqlalchemy.url", f"sqlite:///{self.path.as_posix()}")
        self.engine = sa.create_engine(f"sqlite:///{self.path.as_posix()}")
        self.addCleanup(self.engine.dispose)

    def test_fresh_base_to_head_adds_nullable_columns(self):
        command.upgrade(self.cfg, "head")
        columns = {col["name"]: col for col in sa.inspect(self.engine).get_columns("user_books")}
        for name in ("synopsis_text", "synopsis_checked_at"):
            self.assertIn(name, columns)
            self.assertTrue(columns[name]["nullable"])
        self.assertEqual(ScriptDirectory.from_config(self.cfg).get_heads(), [HEAD])

    def test_upgrade_clears_shared_synopsis_text(self):
        command.upgrade(self.cfg, PREVIOUS)
        columns = sa.inspect(self.engine).get_columns("books")
        values = {"abs_id": "old-book", "synopsis_text": "old", "synopsis_checked_at": 1.0}
        for col in columns:
            if not col["nullable"] and col.get("default") is None and col["name"] not in values:
                values[col["name"]] = 0 if isinstance(col["type"], (sa.Integer, sa.Float)) else "fixture"
        with self.engine.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO books (" + ", ".join(values) + ") VALUES (" +
                ", ".join(":" + key for key in values) + ")"
            ), values)
        command.upgrade(self.cfg, "head")
        with self.engine.connect() as conn:
            row = conn.execute(sa.text(
                "SELECT synopsis_text, synopsis_checked_at FROM books WHERE abs_id='old-book'"
            )).one()
            self.assertEqual(tuple(row), (None, None))

    def test_downgrade_then_upgrade_is_repeatable(self):
        command.upgrade(self.cfg, "head")
        command.downgrade(self.cfg, PREVIOUS)
        downgraded = {col["name"] for col in sa.inspect(self.engine).get_columns("user_books")}
        self.assertNotIn("synopsis_text", downgraded)
        self.assertNotIn("synopsis_checked_at", downgraded)
        command.upgrade(self.cfg, "head")
        columns = {col["name"] for col in sa.inspect(self.engine).get_columns("user_books")}
        self.assertTrue({"synopsis_text", "synopsis_checked_at"} <= columns)
