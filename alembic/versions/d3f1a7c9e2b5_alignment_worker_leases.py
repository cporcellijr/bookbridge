"""Lease columns on jobs for remote alignment workers.

Revision ID: d3f1a7c9e2b5
Revises: b7e528a0c1d2
"""
from alembic import op
import sqlalchemy as sa

revision = "d3f1a7c9e2b5"
down_revision = "b7e528a0c1d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("jobs") as batch:
        batch.add_column(sa.Column("lease_owner", sa.String(length=100), nullable=True))
        batch.add_column(sa.Column("lease_id", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("lease_expires_at", sa.Float(), nullable=True))
        batch.add_column(sa.Column("queue_position_at_claim", sa.Float(), nullable=True))
        batch.add_column(sa.Column("lease_inputs_json", sa.Text(), nullable=True))
    op.create_index("ix_jobs_lease_id", "jobs", ["lease_id"])


def downgrade() -> None:
    op.drop_index("ix_jobs_lease_id", table_name="jobs")
    with op.batch_alter_table("jobs") as batch:
        batch.drop_column("lease_inputs_json")
        batch.drop_column("queue_position_at_claim")
        batch.drop_column("lease_expires_at")
        batch.drop_column("lease_id")
        batch.drop_column("lease_owner")
