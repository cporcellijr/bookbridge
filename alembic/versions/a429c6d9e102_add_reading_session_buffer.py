"""Persist reading session aggregation and pending Grimmory deliveries.

Revision ID: a429c6d9e102
Revises: 2e0a47a3dadd
"""

from alembic import op
import sqlalchemy as sa

revision = "a429c6d9e102"
down_revision = "2e0a47a3dadd"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add buffers without rewriting reading history or existing settings."""
    op.create_table(
        "reading_session_buffers",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("abs_id", sa.String(255), sa.ForeignKey("books.abs_id", ondelete="CASCADE"), nullable=False),
        sa.Column("session_type", sa.String(20), nullable=False),
        sa.Column("leader_client", sa.String(50), nullable=False),
        sa.Column("started_at", sa.Float(), nullable=False),
        sa.Column("last_event_at", sa.Float(), nullable=False),
        sa.Column("accumulated_seconds", sa.Float(), nullable=False),
        sa.Column("start_progress", sa.Float(), nullable=False),
        sa.Column("end_progress", sa.Float(), nullable=False),
        sa.Column("end_location", sa.Text(), nullable=True),
        sa.Column("grimmory_book_id", sa.Integer(), nullable=True),
        sa.Column("grimmory_status", sa.String(20), nullable=False, server_default="disabled"),
        sa.Column("closed_at", sa.Float(), nullable=True),
    )
    op.create_index("ix_reading_session_buffers_user_id", "reading_session_buffers", ["user_id"])
    op.create_index("uq_reading_session_buffer_open", "reading_session_buffers",
                    ["user_id", "abs_id", "session_type"], unique=True,
                    sqlite_where=sa.text("closed_at IS NULL"))


def downgrade() -> None:
    """Remove the buffer table; existing reading history is unaffected."""
    op.drop_table("reading_session_buffers")
