"""Add shared book synopsis cache; an empty result records when it was checked.

Revision ID: h16d20261112
Revises: c585c20261112
Create Date: 2026-11-12
"""
from alembic import op
import sqlalchemy as sa

revision = "h16d20261112"
down_revision = "c585c20261112"
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing = {col["name"] for col in sa.inspect(op.get_bind()).get_columns("books")}
    if "synopsis_text" not in existing:
        op.add_column("books", sa.Column("synopsis_text", sa.Text(), nullable=True))
    if "synopsis_checked_at" not in existing:
        op.add_column("books", sa.Column("synopsis_checked_at", sa.Float(), nullable=True))


def downgrade() -> None:
    existing = {col["name"] for col in sa.inspect(op.get_bind()).get_columns("books")}
    if "synopsis_checked_at" in existing:
        op.drop_column("books", "synopsis_checked_at")
    if "synopsis_text" in existing:
        op.drop_column("books", "synopsis_text")
