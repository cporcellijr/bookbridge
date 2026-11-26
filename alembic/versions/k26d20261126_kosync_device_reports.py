"""Keep each KOReader device's last report per reader and hash, across restarts.

Revision ID: k26d20261126
Revises: d3f1a7c9e2b5
Create Date: 2026-11-26
"""
from alembic import op
import sqlalchemy as sa

revision = "k26d20261126"
down_revision = "d3f1a7c9e2b5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "kosync_device_reports" not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table(
            "kosync_device_reports",
            sa.Column("document_hash", sa.String(length=32), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), primary_key=True),
            sa.Column("device_id", sa.String(length=64), primary_key=True),
            sa.Column("progress", sa.String(length=512), nullable=False),
            sa.Column("percentage", sa.Float(), nullable=False),
        )


def downgrade() -> None:
    if "kosync_device_reports" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("kosync_device_reports")
