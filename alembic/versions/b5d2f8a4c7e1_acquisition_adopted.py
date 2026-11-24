"""Mark acquisition requests adopted from the reader's own BookOrbit requests.

Revision ID: b5d2f8a4c7e1
Revises: a7e3c9d1b5f2
Create Date: 2026-11-11
"""
from alembic import op
import sqlalchemy as sa

revision = "b5d2f8a4c7e1"
down_revision = "a7e3c9d1b5f2"
branch_labels = None
depends_on = None

_TABLE = "acquisition_requests"


def upgrade() -> None:
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns(_TABLE)}
    if "adopted" not in existing:
        op.add_column(_TABLE, sa.Column("adopted", sa.Boolean(), nullable=False, server_default="0"))


def downgrade() -> None:
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns(_TABLE)}
    if "adopted" in existing:
        with op.batch_alter_table(_TABLE) as batch:
            batch.drop_column("adopted")
