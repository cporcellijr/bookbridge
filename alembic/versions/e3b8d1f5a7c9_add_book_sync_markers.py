"""Record the leader of each user's last sync per book (library card glow).

Revision ID: e3b8d1f5a7c9
Revises: b7e528a0c1d2
"""
from alembic import op
import sqlalchemy as sa

revision = "e3b8d1f5a7c9"
down_revision = "b7e528a0c1d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "book_sync_markers",
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("abs_id", sa.String(length=255), sa.ForeignKey("books.abs_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("leader_client", sa.String(length=50), nullable=False),
        sa.Column("synced_at", sa.Float(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("book_sync_markers")
