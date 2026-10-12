"""never reissue a deleted user's id

Without AUTOINCREMENT SQLite hands the highest deleted users.id to the next
account, which then inherits the deleted account's browser sessions and any row
still stamped with that id. Rebuild users with AUTOINCREMENT and start the
sequence past every user id any table still references.

Revision ID: 8ef64bf8c29e
Revises: ccf04b7f7b3e
Create Date: 2026-10-12
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "8ef64bf8c29e"
down_revision: Union[str, Sequence[str], None] = "ccf04b7f7b3e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _users_sql(bind) -> str:
    return bind.execute(sa.text(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'users'"
    )).scalar() or ""


def upgrade() -> None:
    bind = op.get_bind()
    if "AUTOINCREMENT" not in _users_sql(bind).upper():
        with op.batch_alter_table("users", recreate="always",
                                  table_kwargs={"sqlite_autoincrement": True}):
            pass

    inspector = sa.inspect(bind)
    floor = 0
    for table in inspector.get_table_names():
        if table == "users" or any(c["name"] == "user_id" for c in inspector.get_columns(table)):
            column = "id" if table == "users" else "user_id"
            floor = max(floor, int(bind.execute(sa.text(f'SELECT MAX("{column}") FROM "{table}"')).scalar() or 0))
    seq = bind.execute(sa.text("SELECT seq FROM sqlite_sequence WHERE name = 'users'")).scalar()
    if seq is None:
        bind.execute(sa.text("INSERT INTO sqlite_sequence (name, seq) VALUES ('users', :floor)"), {"floor": floor})
    elif seq < floor:
        bind.execute(sa.text("UPDATE sqlite_sequence SET seq = :floor WHERE name = 'users'"), {"floor": floor})


def downgrade() -> None:
    with op.batch_alter_table("users", recreate="always"):
        pass
