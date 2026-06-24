"""add user_books membership links (shared catalog, per-user visibility)

A book can be matched/claimed by several users. Visibility (dashboard + koplugin
manifest) keys off these links rather than a single owner. Backfills links from
the existing Book.user_id owner and from any per-user progress rows, so the
current library stays visible to whoever already had it.

Revision ID: d7f0a2c4e6b8
Revises: c5d7e9f1a3b4
Create Date: 2026-06-22
"""

from alembic import op
import sqlalchemy as sa


revision = "d7f0a2c4e6b8"
down_revision = "c5d7e9f1a3b4"
branch_labels = None
depends_on = None


def _tables(inspector) -> set:
    return set(inspector.get_table_names())


def _indexes(inspector, table_name: str) -> set:
    if table_name not in inspector.get_table_names():
        return set()
    return {idx["name"] for idx in inspector.get_indexes(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "user_books" not in _tables(inspector):
        op.create_table(
            "user_books",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("abs_id", sa.String(length=255), sa.ForeignKey("books.abs_id", ondelete="CASCADE"), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=True),
        )

    inspector = sa.inspect(bind)
    idx = _indexes(inspector, "user_books")
    if "ix_user_books_user_id" not in idx:
        op.create_index("ix_user_books_user_id", "user_books", ["user_id"])
    if "ix_user_books_abs_id" not in idx:
        op.create_index("ix_user_books_abs_id", "user_books", ["abs_id"])
    if "ix_user_books_user_abs" not in idx:
        op.create_index("ix_user_books_user_abs", "user_books", ["user_id", "abs_id"], unique=True)

    # Backfill: link each book to its current owner, and to any user who already
    # has a progress row for it. INSERT OR IGNORE keeps the unique pair idempotent.
    tables = _tables(inspector)
    if "books" in tables:
        bind.execute(sa.text(
            "INSERT OR IGNORE INTO user_books (user_id, abs_id, created_at) "
            "SELECT user_id, abs_id, CURRENT_TIMESTAMP FROM books WHERE user_id IS NOT NULL"
        ))
    if "states" in tables:
        bind.execute(sa.text(
            "INSERT OR IGNORE INTO user_books (user_id, abs_id, created_at) "
            "SELECT DISTINCT s.user_id, s.abs_id, CURRENT_TIMESTAMP FROM states s "
            "JOIN books b ON b.abs_id = s.abs_id WHERE s.user_id IS NOT NULL"
        ))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "user_books" in _tables(inspector):
        op.drop_table("user_books")
