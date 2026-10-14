"""snapshot the book title on reading sessions so history survives mapping deletion

Revision ID: 9b3e5d7f1a42
Revises: 4d8a1f6c9b20
Create Date: 2026-10-14
"""

from alembic import op
import sqlalchemy as sa


revision = "9b3e5d7f1a42"
down_revision = "4d8a1f6c9b20"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("reading_sessions", sa.Column("book_title", sa.String(length=500), nullable=True))
    op.execute(
        "UPDATE reading_sessions SET book_title = ("
        "SELECT COALESCE(NULLIF(books.abs_title, ''), NULLIF(books.audio_title, '')) FROM books "
        "WHERE books.abs_id = reading_sessions.abs_id)"
    )


def downgrade() -> None:
    op.drop_column("reading_sessions", "book_title")
