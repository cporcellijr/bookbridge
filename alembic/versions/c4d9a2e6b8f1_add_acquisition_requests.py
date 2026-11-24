"""Track books a reader asked a service to fetch (acquisition requests).

Revision ID: c4d9a2e6b8f1
Revises: e3b8d1f5a7c9
Create Date: 2026-11-11
"""
from alembic import op
import sqlalchemy as sa

revision = "c4d9a2e6b8f1"
down_revision = "e3b8d1f5a7c9"
branch_labels = None
depends_on = None

_TABLE = "acquisition_requests"


def upgrade() -> None:
    if _TABLE in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(), nullable=True),
        sa.Column("service", sa.String(length=30), nullable=False),
        sa.Column("media_kind", sa.String(length=20), nullable=False),
        sa.Column("remote_kind", sa.String(length=20), nullable=True),
        sa.Column("remote_id", sa.String(length=255), nullable=True),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("authors", sa.String(length=500), nullable=True),
        sa.Column("isbn13", sa.String(length=20), nullable=True),
        sa.Column("series_name", sa.String(length=500), nullable=True),
        sa.Column("series_index", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="waiting"),
        sa.Column("remote_status", sa.String(length=50), nullable=True),
        sa.Column("status_detail", sa.Text(), nullable=True),
        sa.Column("partner_source", sa.String(length=50), nullable=True),
        sa.Column("partner_source_id", sa.String(length=255), nullable=True),
        sa.Column("partner_filename", sa.String(length=500), nullable=True),
        sa.Column("group_key", sa.String(length=64), nullable=True),
        sa.Column("arrived_source", sa.String(length=50), nullable=True),
        sa.Column("arrived_source_id", sa.String(length=255), nullable=True),
        sa.Column("arrived_filename", sa.String(length=500), nullable=True),
        sa.Column("linked_abs_id", sa.String(length=255), nullable=True),
        sa.Column("dismissed", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_acquisition_requests_user_id", _TABLE, ["user_id"])


def downgrade() -> None:
    if _TABLE in set(sa.inspect(op.get_bind()).get_table_names()):
        op.drop_table(_TABLE)
