"""add nullable user_id (owner) to books and backfill to the default admin

Multi-user: matches gain an owner so the dashboard and KOReader manifest can be
scoped per user. The catalog row stays shared at the schema level; ownership is
the visibility/serving key. Existing rows backfill to the first admin user (or
the first user) so the pre-multi-user library stays visible to the operator.

Revision ID: c5d7e9f1a3b4
Revises: f7a1b9c3d2e8
Create Date: 2026-06-22
"""

from alembic import op
import sqlalchemy as sa


revision = "c5d7e9f1a3b4"
down_revision = "f7a1b9c3d2e8"
branch_labels = None
depends_on = None


def _get_columns(inspector, table_name: str) -> set:
    if table_name not in inspector.get_table_names():
        return set()
    return {c["name"] for c in inspector.get_columns(table_name)}


def _get_indexes(inspector, table_name: str) -> set:
    if table_name not in inspector.get_table_names():
        return set()
    return {idx["name"] for idx in inspector.get_indexes(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "books" not in inspector.get_table_names():
        return

    if "user_id" not in _get_columns(inspector, "books"):
        op.add_column("books", sa.Column("user_id", sa.Integer(), nullable=True))

    inspector = sa.inspect(bind)
    if "ix_books_user_id" not in _get_indexes(inspector, "books"):
        op.create_index("ix_books_user_id", "books", ["user_id"])

    # Backfill: hand the pre-existing library to the operator (first admin, else
    # first user). Only touch rows that have no owner yet.
    if "users" in inspector.get_table_names():
        admin_id = bind.execute(
            sa.text(
                "SELECT id FROM users "
                "ORDER BY CASE WHEN role = 'admin' THEN 0 ELSE 1 END, id "
                "LIMIT 1"
            )
        ).scalar()
        if admin_id is not None:
            bind.execute(
                sa.text("UPDATE books SET user_id = :uid WHERE user_id IS NULL"),
                {"uid": admin_id},
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "ix_books_user_id" in _get_indexes(inspector, "books"):
        op.drop_index("ix_books_user_id", table_name="books")
    if "user_id" in _get_columns(inspector, "books"):
        with op.batch_alter_table("books") as batch_op:
            batch_op.drop_column("user_id")
