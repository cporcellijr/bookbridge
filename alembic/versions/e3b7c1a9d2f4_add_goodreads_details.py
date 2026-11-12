"""Add Goodreads book matches and metadata.

Installs that ran the Goodreads preview branch already have this table, so it is
created only when missing.

Revision ID: e3b7c1a9d2f4
Revises: b5d2f8a4c7e1
Create Date: 2026-11-12
"""

from alembic import op
import sqlalchemy as sa


revision = "e3b7c1a9d2f4"
down_revision = "b5d2f8a4c7e1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("goodreads_details"):
        return
    op.create_table(
        "goodreads_details",
        sa.Column("abs_id", sa.String(255), sa.ForeignKey("books.abs_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("goodreads_book_id", sa.String(255), nullable=False),
        sa.Column("goodreads_url", sa.String(1000)),
        sa.Column("goodreads_pages", sa.Integer()),
        sa.Column("goodreads_rating", sa.Float()),
        sa.Column("goodreads_review_count", sa.Integer()),
        sa.Column("goodreads_rating_updated_at", sa.Float()),
        sa.Column("isbn", sa.String(255)),
        sa.Column("asin", sa.String(255)),
        sa.Column("matched_by", sa.String(50)),
        sa.Column("created_at", sa.DateTime()),
        sa.Column("updated_at", sa.DateTime()),
    )


def downgrade() -> None:
    op.drop_table("goodreads_details")
