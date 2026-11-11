"""Track when a downloading acquisition request last made progress.

Revision ID: a7e3c9d1b5f2
Revises: c4d9a2e6b8f1
Create Date: 2026-11-11
"""
from alembic import op
import sqlalchemy as sa

revision = "a7e3c9d1b5f2"
down_revision = "c4d9a2e6b8f1"
branch_labels = None
depends_on = None

_TABLE = "acquisition_requests"
_COLUMNS = (
    ("progress_mark", sa.String(length=64)),
    ("progress_changed_at", sa.DateTime()),
)


def upgrade() -> None:
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns(_TABLE)}
    for name, type_ in _COLUMNS:
        if name not in existing:
            op.add_column(_TABLE, sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns(_TABLE)}
    with op.batch_alter_table(_TABLE) as batch:
        for name, _ in _COLUMNS:
            if name in existing:
                batch.drop_column(name)
