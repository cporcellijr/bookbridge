"""add stump_media_id column to books

Revision ID: f7a3b5c9d1e2
Revises: e3f1a4b8c2d9
Create Date: 2026-03-18
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f7a3b5c9d1e2"
down_revision: Union[str, Sequence[str], None] = "e3f1a4b8c2d9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {c["name"] for c in inspector.get_columns(table_name)}
    if column.name not in columns:
        op.add_column(table_name, column)


def _drop_column_if_present(table_name: str, column_name: str) -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {c["name"] for c in inspector.get_columns(table_name)}
    if column_name in columns:
        op.drop_column(table_name, column_name)


def upgrade() -> None:
    _add_column_if_missing(
        "books",
        sa.Column("stump_media_id", sa.String(length=255), nullable=True),
    )
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    indexes = {idx["name"] for idx in inspector.get_indexes("books")}
    if "ix_books_stump_media_id" not in indexes:
        op.create_index("ix_books_stump_media_id", "books", ["stump_media_id"])


def downgrade() -> None:
    try:
        op.drop_index("ix_books_stump_media_id", table_name="books")
    except Exception:
        pass
    _drop_column_if_present("books", "stump_media_id")
