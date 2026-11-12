"""Real acquisition-job migrations preserve the existing request history."""

import importlib.util
import tempfile
import unittest
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

from src.db.models import AcquisitionJob


pytestmark = pytest.mark.real_database_migrations
ROOT = Path(__file__).resolve().parents[1]
HEAD = "f6b585a12c34"
PREVIOUS = "e3b7c1a9d2f4"


class TestAcquisitionJobsMigration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "migration.db"
        self.cfg = Config(str(ROOT / "alembic.ini"))
        self.cfg.set_main_option("script_location", str(ROOT / "alembic"))
        self.cfg.set_main_option("sqlalchemy.url", f"sqlite:///{self.path.as_posix()}")
        self.engine = sa.create_engine(f"sqlite:///{self.path.as_posix()}")
        self.addCleanup(self.engine.dispose)

    def seed_previous(self):
        command.upgrade(self.cfg, PREVIOUS)
        with self.engine.begin() as conn:
            conn.execute(sa.text("INSERT INTO users (id, username, role, active) VALUES (1, 'reader', 'user', 1)"))
            conn.execute(sa.text(
                "INSERT INTO acquisition_requests "
                "(id, user_id, service, media_kind, remote_id, title, status, group_key, dismissed) "
                "VALUES (42, 1, 'BookOrbit', 'audiobook', '21', 'Historical book', 'paired', 'legacy-pair', 1)"
            ))

    def history(self):
        with self.engine.connect() as conn:
            return conn.execute(sa.text("SELECT * FROM acquisition_requests ORDER BY id")).all()

    def test_fresh_base_to_head_matches_the_model_and_has_one_head(self):
        command.upgrade(self.cfg, "head")
        inspector = sa.inspect(self.engine)
        self.assertEqual({c["name"] for c in inspector.get_columns("acquisition_jobs")},
                         set(AcquisitionJob.__table__.columns.keys()))
        self.assertIn("ix_acquisition_jobs_user_id", {i["name"] for i in inspector.get_indexes("acquisition_jobs")})
        self.assertEqual(ScriptDirectory.from_config(self.cfg).get_heads(), [HEAD])
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar(), HEAD)

    def test_upgrade_keeps_dismissed_and_grouped_legacy_history_unchanged(self):
        self.seed_previous()
        before = self.history()
        command.upgrade(self.cfg, "head")
        self.assertEqual(self.history(), before)
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(sa.text("SELECT count(*) FROM acquisition_jobs")).scalar(), 0)

    def test_repeated_upgrade_keeps_existing_job_rows(self):
        self.seed_previous()
        command.upgrade(self.cfg, "head")
        with self.engine.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO acquisition_jobs (id, user_id, title, requested_ebook) "
                "VALUES (:id, 1, 'Saved intent', 1)"
            ), {"id": "a" * 32})
        path = ROOT / "alembic" / "versions" / "f6b585a12c34_add_acquisition_jobs.py"
        spec = importlib.util.spec_from_file_location("acquisition_jobs_migration", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        with self.engine.begin() as conn:
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(sa.text("SELECT title FROM acquisition_jobs")).scalar(), "Saved intent")
        self.assertEqual(len(self.history()), 1)

    def test_downgrade_removes_only_the_new_table(self):
        self.seed_previous()
        before = self.history()
        command.upgrade(self.cfg, "head")
        command.downgrade(self.cfg, PREVIOUS)
        self.assertNotIn("acquisition_jobs", sa.inspect(self.engine).get_table_names())
        self.assertEqual(self.history(), before)
