"""Persist acquisition intent independently of provider request receipts.

Revision ID: f6b585a12c34
Revises: e3b7c1a9d2f4
Create Date: 2026-11-12
"""
from alembic import op
import sqlalchemy as sa


revision = "f6b585a12c34"
down_revision = "e3b7c1a9d2f4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("acquisition_jobs"):
        return
    op.create_table(
        "acquisition_jobs",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("authors", sa.String(500)),
        sa.Column("isbn13", sa.String(20)),
        sa.Column("series_name", sa.String(500)),
        sa.Column("series_index", sa.Integer()),
        sa.Column("requested_ebook", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("requested_audiobook", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("ebook_source", sa.String(50)),
        sa.Column("ebook_source_id", sa.String(255)),
        sa.Column("ebook_filename", sa.String(500)),
        sa.Column("audio_source", sa.String(50)),
        sa.Column("audio_source_id", sa.String(255)),
        sa.Column("created_at", sa.DateTime()),
        sa.Column("updated_at", sa.DateTime()),
        sa.Column("dismissed", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_acquisition_jobs_user_id", "acquisition_jobs", ["user_id"])


def downgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("acquisition_jobs"):
        op.drop_table("acquisition_jobs")
