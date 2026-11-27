"""Add Fable book matches and metadata.

Revision ID: f4a8c2e6b1d9
Revises: a481d26f019e
Create Date: 2026-11-26
"""

from alembic import op
import sqlalchemy as sa


revision = "f4a8c2e6b1d9"
down_revision = "a481d26f019e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fable_details",
        sa.Column("abs_id", sa.String(255), sa.ForeignKey("books.abs_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("fable_book_id", sa.String(255), nullable=False),
        sa.Column("fable_url", sa.String(1000)),
        sa.Column("fable_pages", sa.Integer()),
        sa.Column("fable_rating", sa.Float()),
        sa.Column("fable_review_count", sa.Integer()),
        sa.Column("fable_rating_updated_at", sa.Float()),
        sa.Column("isbn", sa.String(255)),
        sa.Column("asin", sa.String(255)),
        sa.Column("matched_by", sa.String(50)),
        sa.Column("created_at", sa.DateTime()),
        sa.Column("updated_at", sa.DateTime()),
    )


def downgrade() -> None:
    op.drop_table("fable_details")
