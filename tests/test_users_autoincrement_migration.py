"""The users AUTOINCREMENT migration: a deleted account's id is never reissued,
including ids an older delete left dangling on per-user rows."""

import importlib.util
import unittest
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


class TestUsersAutoincrementMigration(unittest.TestCase):
    def setUp(self) -> None:
        path = Path(__file__).resolve().parents[1] / "alembic/versions/8ef64bf8c29e_users_autoincrement.py"
        spec = importlib.util.spec_from_file_location("users_autoincrement_migration", path)
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)
        self.engine = sa.create_engine("sqlite:///:memory:")
        with self.engine.begin() as conn:
            conn.execute(sa.text(
                "CREATE TABLE users (id INTEGER NOT NULL PRIMARY KEY, username VARCHAR(255) NOT NULL, "
                "role VARCHAR(20) NOT NULL DEFAULT 'user', active INTEGER NOT NULL DEFAULT '1')"
            ))
            conn.execute(sa.text("CREATE UNIQUE INDEX ix_users_username ON users (username)"))
            conn.execute(sa.text("CREATE TABLE states (id INTEGER PRIMARY KEY, user_id INTEGER)"))
            conn.execute(sa.text("INSERT INTO users (id, username, role) VALUES (1, 'admin', 'admin'), (2, 'reader', 'user')"))
            # A pre-fix delete of user 5 left this row behind.
            conn.execute(sa.text("INSERT INTO states (user_id) VALUES (5)"))

    def tearDown(self) -> None:
        self.engine.dispose()

    def _upgrade(self) -> None:
        with self.engine.begin() as conn:
            old_op = self.mod.op
            try:
                self.mod.op = Operations(MigrationContext.configure(conn))
                self.mod.upgrade()
            finally:
                self.mod.op = old_op

    def _insert_user(self, username: str) -> int:
        with self.engine.begin() as conn:
            return conn.execute(sa.text("INSERT INTO users (username) VALUES (:u)"), {"u": username}).lastrowid

    def test_upgrade_preserves_users_and_skips_every_referenced_id(self) -> None:
        self._upgrade()
        with self.engine.begin() as conn:
            sql = conn.execute(sa.text("SELECT sql FROM sqlite_master WHERE name = 'users'")).scalar()
            rows = conn.execute(sa.text("SELECT id, username, role FROM users ORDER BY id")).all()
        self.assertIn("AUTOINCREMENT", sql.upper())
        self.assertEqual([tuple(r) for r in rows], [(1, "admin", "admin"), (2, "reader", "user")])
        self.assertIn("ix_users_username", {i["name"] for i in sa.inspect(self.engine).get_indexes("users")})
        self.assertEqual(self._insert_user("next"), 6)

    def test_deleted_highest_id_is_not_reused_after_upgrade(self) -> None:
        self._upgrade()
        newest = self._insert_user("newest")
        with self.engine.begin() as conn:
            conn.execute(sa.text("DELETE FROM users WHERE id = :id"), {"id": newest})
        self.assertEqual(self._insert_user("after-delete"), newest + 1)

    def test_upgrade_is_idempotent(self) -> None:
        self._upgrade()
        self._upgrade()
        self.assertEqual(self._insert_user("next"), 6)


if __name__ == "__main__":
    unittest.main()
