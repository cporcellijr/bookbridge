"""Persist per-user EPUB/audio pre-flight evidence (#528).

Revision ID: b7e528a0c1d2
Revises: c4e9a2f7b6d1
"""
from alembic import op
import sqlalchemy as sa

revision = "b7e528a0c1d2"
down_revision = "c4e9a2f7b6d1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "transcription_preflights",
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("abs_id", sa.String(length=255), sa.ForeignKey("books.abs_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("pairing_key", sa.String(length=64), nullable=False),
        sa.Column("identity", sa.String(length=64), nullable=False),
        sa.Column("evidence_json", sa.Text(), nullable=False),
        sa.Column("acknowledged_at", sa.Float(), nullable=True),
        sa.Column("updated_at", sa.Float(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("transcription_preflights")
