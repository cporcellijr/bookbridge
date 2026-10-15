"""add nullable users.language for the UI locale

The per-user interface language. NULL means "never chosen": the locale selector
falls back to the request's Accept-Language, then English. Kept on `users`
rather than in `user_credentials` because it is an account display preference,
not a service credential.

Revision ID: b8f3d6a1c9e4
Revises: a8c7e0d1b529
Create Date: 2026-10-15
"""

from alembic import op
import sqlalchemy as sa


revision = "b8f3d6a1c9e4"
down_revision = "a8c7e0d1b529"
branch_labels = None
depends_on = None


def _get_columns(inspector, table_name: str) -> set:
    if table_name not in inspector.get_table_names():
        return set()
    return {c["name"] for c in inspector.get_columns(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "language" not in _get_columns(inspector, "users"):
        op.add_column("users", sa.Column("language", sa.String(length=10), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "language" in _get_columns(inspector, "users"):
        with op.batch_alter_table("users") as batch_op:
            batch_op.drop_column("language")
