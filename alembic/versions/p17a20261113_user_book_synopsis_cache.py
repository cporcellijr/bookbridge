"""Move synopsis cache to reader membership; clear shared synopsis data.

Revision ID: p17a20261113
Revises: d585c20261112
Create Date: 2026-11-13
"""
from alembic import op
import sqlalchemy as sa

revision = "p17a20261113"
down_revision = "d585c20261112"
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing = {col["name"] for col in sa.inspect(op.get_bind()).get_columns("user_books")}
    if "synopsis_text" not in existing:
        op.add_column("user_books", sa.Column("synopsis_text", sa.Text(), nullable=True))
    if "synopsis_checked_at" not in existing:
        op.add_column("user_books", sa.Column("synopsis_checked_at", sa.Float(), nullable=True))
    op.execute("UPDATE books SET synopsis_text = NULL, synopsis_checked_at = NULL")


def downgrade() -> None:
    existing = {col["name"] for col in sa.inspect(op.get_bind()).get_columns("user_books")}
    if "synopsis_checked_at" in existing:
        op.drop_column("user_books", "synopsis_checked_at")
    if "synopsis_text" in existing:
        op.drop_column("user_books", "synopsis_text")
