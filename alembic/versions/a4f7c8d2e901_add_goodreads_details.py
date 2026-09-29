"""Add Goodreads book matches and metadata.

Revision ID: a4f7c8d2e901
Revises: ccf04b7f7b3e
Create Date: 2026-09-29
"""

from alembic import op
import sqlalchemy as sa


revision = "a4f7c8d2e901"
down_revision = "ccf04b7f7b3e"
branch_labels = None
depends_on = None


def upgrade() -> None:
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
