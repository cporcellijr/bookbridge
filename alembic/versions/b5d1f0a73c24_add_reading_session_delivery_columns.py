"""Add BookOrbit delivery tracking and delivery attempt counter to reading session buffer.

Revision ID: b5d1f0a73c24
Revises: a429c6d9e102
"""

from alembic import op
import sqlalchemy as sa

revision = "b5d1f0a73c24"
down_revision = "a429c6d9e102"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add BookOrbit columns and delivery attempt counter to reading_session_buffers."""
    op.add_column(
        "reading_session_buffers",
        sa.Column("bookorbit_book_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "reading_session_buffers",
        sa.Column("bookorbit_candidate_ids", sa.Text(), nullable=True),
    )
    op.add_column(
        "reading_session_buffers",
        sa.Column(
            "bookorbit_status",
            sa.String(20),
            nullable=False,
            server_default="disabled",
        ),
    )
    op.add_column(
        "reading_session_buffers",
        sa.Column(
            "delivery_attempts",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    """Remove BookOrbit columns and delivery attempt counter from reading_session_buffers."""
    with op.batch_alter_table("reading_session_buffers") as batch_op:
        batch_op.drop_column("bookorbit_book_id")
        batch_op.drop_column("bookorbit_candidate_ids")
        batch_op.drop_column("bookorbit_status")
        batch_op.drop_column("delivery_attempts")