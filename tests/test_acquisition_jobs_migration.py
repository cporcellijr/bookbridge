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

from src.db.models import AcquisitionJob, AcquisitionRequest


pytestmark = pytest.mark.real_database_migrations
ROOT = Path(__file__).resolve().parents[1]
HEAD = "c4e7a2b9d1f3"
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
            return conn.execute(sa.text("SELECT id, user_id, service, media_kind, remote_id, title, status, group_key, dismissed FROM acquisition_requests ORDER BY id")).all()

    def test_fresh_base_to_head_matches_the_model_and_has_one_head(self):
        command.upgrade(self.cfg, "head")
        inspector = sa.inspect(self.engine)
        self.assertEqual({c["name"] for c in inspector.get_columns("acquisition_jobs")},
                         set(AcquisitionJob.__table__.columns.keys()))
        self.assertEqual({c["name"] for c in inspector.get_columns("acquisition_requests")},
                         set(AcquisitionRequest.__table__.columns.keys()))
        self.assertIn("ix_acquisition_jobs_user_id", {i["name"] for i in inspector.get_indexes("acquisition_jobs")})
        book_columns = {col["name"] for col in inspector.get_columns("books")}
        self.assertIn("synopsis_text", book_columns)
        self.assertIn("synopsis_checked_at", book_columns)
        self.assertTrue(next(col for col in inspector.get_columns("books")
                             if col["name"] == "synopsis_checked_at")["nullable"])
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

    def test_phase_c_additive_migration_can_upgrade_twice_and_downgrade_without_erasing_history(self):
        self.seed_previous()
        before = self.history()
        command.upgrade(self.cfg, "f6b585a12c34")
        path = ROOT / "alembic" / "versions" / "c585c20261112_acquisition_submission_decisions.py"
        spec = importlib.util.spec_from_file_location("acquisition_phase_c_migration", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        for _ in range(2):
            with self.engine.begin() as conn:
                with Operations.context(MigrationContext.configure(conn)):
                    migration.upgrade()
        inspector = sa.inspect(self.engine)
        self.assertIn("decision_action", {c["name"] for c in inspector.get_columns("acquisition_requests")})
        self.assertIn("audio_submission_state", {c["name"] for c in inspector.get_columns("acquisition_jobs")})
        with self.engine.begin() as conn:
            with Operations.context(MigrationContext.configure(conn)):
                migration.downgrade()
        self.assertNotIn("decision_action", {c["name"] for c in sa.inspect(self.engine).get_columns("acquisition_requests")})
        self.assertEqual(self.history(), before)

    def test_phase_d_columns_upgrade_twice_and_downgrade_without_history_loss(self):
        self.seed_previous()
        before = self.history()
        command.upgrade(self.cfg, "c585c20261112")
        with self.engine.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO acquisition_jobs (id, user_id, title, requested_audiobook, "
                "audio_submission_state) VALUES (:id, 1, 'Saved intent', 1, 'uncertain')"
            ), {"id": "a" * 32})
        path = ROOT / "alembic" / "versions" / "d585c20261112_acquisition_library_scan_state.py"
        spec = importlib.util.spec_from_file_location("acquisition_phase_d_migration", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        for _ in range(2):
            with self.engine.begin() as conn:
                with Operations.context(MigrationContext.configure(conn)):
                    migration.upgrade()
        self.assertEqual(
            {c["name"] for c in sa.inspect(self.engine).get_columns("acquisition_jobs")},
            set(AcquisitionJob.__table__.columns.keys()),
        )
        with self.engine.begin() as conn:
            conn.execute(sa.text(
                "UPDATE acquisition_jobs SET scan_state='accepted', scan_library_id='7', "
                "scan_job_id='42', scan_attempts=1 WHERE id=:id"
            ), {"id": "a" * 32})
        self.assertEqual(self.history(), before)
        with self.engine.begin() as conn:
            with Operations.context(MigrationContext.configure(conn)):
                migration.downgrade()
        cols = {c["name"] for c in sa.inspect(self.engine).get_columns("acquisition_jobs")}
        self.assertNotIn("scan_job_id", cols)
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(sa.text(
                "SELECT audio_submission_state FROM acquisition_jobs WHERE id=:id"
            ), {"id": "a" * 32}).scalar(), "uncertain")
        self.assertEqual(self.history(), before)

    def test_downgrade_removes_only_the_new_table(self):
        self.seed_previous()
        before = self.history()
        command.upgrade(self.cfg, "head")
        command.downgrade(self.cfg, PREVIOUS)
        self.assertNotIn("acquisition_jobs", sa.inspect(self.engine).get_table_names())
        self.assertEqual(self.history(), before)
