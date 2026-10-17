"""Store ordered positions for pending book alignment.

Revision ID: a8c7e0d1b529
Revises: 9b3e5d7f1a42
"""
from alembic import op
import sqlalchemy as sa

revision = "a8c7e0d1b529"
down_revision = "9b3e5d7f1a42"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("books", sa.Column("alignment_queue_position", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("books", "alignment_queue_position")
