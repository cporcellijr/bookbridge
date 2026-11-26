"""The KoSync device-report table: built at head as its model builds it, dropped on downgrade."""

import tempfile
import unittest
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from src.db.models import Base

pytestmark = pytest.mark.real_database_migrations
ROOT = Path(__file__).resolve().parents[1]
PREVIOUS = "d3f1a7c9e2b5"
TABLE = "kosync_device_reports"


def _shape(inspector):
    return ([(c["name"], str(c["type"]), c["nullable"]) for c in inspector.get_columns(TABLE)],
            inspector.get_pk_constraint(TABLE)["constrained_columns"],
            [(fk["constrained_columns"], fk["referred_table"], fk["referred_columns"], fk.get("options"))
             for fk in inspector.get_foreign_keys(TABLE)])


class TestKosyncDeviceReportsMigration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "migration.db"
        self.cfg = Config(str(ROOT / "alembic.ini"))
        self.cfg.set_main_option("script_location", str(ROOT / "alembic"))
        self.cfg.set_main_option("sqlalchemy.url", f"sqlite:///{self.path.as_posix()}")
        self.engine = sa.create_engine(f"sqlite:///{self.path.as_posix()}")
        self.addCleanup(self.engine.dispose)

    def test_fresh_base_to_head_builds_the_table_as_the_model_does(self):
        command.upgrade(self.cfg, "head")
        model_engine = sa.create_engine("sqlite://")
        self.addCleanup(model_engine.dispose)
        Base.metadata.create_all(model_engine)
        self.assertEqual(_shape(sa.inspect(self.engine)), _shape(sa.inspect(model_engine)))
        # Every column is NOT NULL: a report always has a locator, "" when the device sent none.
        self.assertEqual([c for c in _shape(sa.inspect(self.engine))[0] if c[2]], [])

    def test_downgrade_drops_the_table_and_upgrade_builds_it_again(self):
        command.upgrade(self.cfg, "head")
        with self.engine.begin() as conn:
            conn.execute(sa.text("INSERT INTO users (id, username, role, active) VALUES (1, 'reader', 'user', 1)"))
            conn.execute(sa.text(
                f"INSERT INTO {TABLE} (document_hash, user_id, device_id, progress, percentage) "
                "VALUES ('0123456789abcdef0123456789abcdef', 1, 'kobo-1', '/body/p[1].0', 0.5)"
            ))
        command.downgrade(self.cfg, PREVIOUS)
        self.assertNotIn(TABLE, sa.inspect(self.engine).get_table_names())
        command.upgrade(self.cfg, "head")
        self.assertIn(TABLE, sa.inspect(self.engine).get_table_names())


if __name__ == "__main__":
    unittest.main()
