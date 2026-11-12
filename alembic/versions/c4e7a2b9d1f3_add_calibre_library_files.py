"""add calibre_library_files: the Calibre book each found EPUB name belongs to

A mapping stores only its EPUB's file name. Editing a book's title in Calibre or
CWA renames the file, and the mapping could never find it again. This table
records the Calibre book id a name was found as, so the parser can follow a
missing name to the book's current file through the library's metadata.db.

Revision ID: c4e7a2b9d1f3
Revises: e3b8d1f5a7c9
Create Date: 2026-11-11
"""

import sqlalchemy as sa
from alembic import op


revision = "c4e7a2b9d1f3"
down_revision = "e3b8d1f5a7c9"
branch_labels = None
depends_on = None

_TABLE = "calibre_library_files"


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if _TABLE in inspector.get_table_names():
        return
    op.create_table(
        _TABLE,
        sa.Column("filename", sa.String(length=500), primary_key=True),
        sa.Column("calibre_book_id", sa.String(length=64), nullable=True),
        sa.Column("updated_at", sa.Float(), nullable=False),
    )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if _TABLE in inspector.get_table_names():
        op.drop_table(_TABLE)
