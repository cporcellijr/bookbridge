"""add durable ebook recovery identity to jobs

Revision ID: 4d8a1f6c9b20
Revises: 7c1e4f9a2b6d
Create Date: 2026-10-13
"""

from alembic import op
import sqlalchemy as sa


revision = "4d8a1f6c9b20"
down_revision = "7c1e4f9a2b6d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("recovery_identity", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "recovery_identity")
