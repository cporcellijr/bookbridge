"""add shelf watch tables

Revision ID: f3c8e2a5b9d4
Revises: e9b1c4d7a2f0
Create Date: 2026-05-13
"""

from alembic import op
import sqlalchemy as sa

revision = "f3c8e2a5b9d4"
down_revision = "e9b1c4d7a2f0"
branch_labels = None
depends_on = None


def _get_columns(inspector, table_name: str) -> set:
    if table_name not in inspector.get_table_names():
        return set()
    return {c["name"] for c in inspector.get_columns(table_name)}


def _get_indexes(inspector, table_name: str) -> set:
    if table_name not in inspector.get_table_names():
        return set()
    return {idx["name"] for idx in inspector.get_indexes(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # 1. Extend pending_suggestions with origin metadata
    cols = _get_columns(inspector, "pending_suggestions")
    if "origin" not in cols:
        op.add_column("pending_suggestions", sa.Column("origin", sa.String(length=50), nullable=True))
    if "origin_metadata_json" not in cols:
        op.add_column("pending_suggestions", sa.Column("origin_metadata_json", sa.Text(), nullable=True))

    inspector = sa.inspect(bind)
    pending_indexes = _get_indexes(inspector, "pending_suggestions")
    if "ix_pending_suggestions_origin" not in pending_indexes:
        op.create_index("ix_pending_suggestions_origin", "pending_suggestions", ["origin"])

    # 2. Create shelf_watch_scans table
    if "shelf_watch_scans" not in inspector.get_table_names():
        op.create_table(
            "shelf_watch_scans",
            sa.Column("grimmory_book_id", sa.String(length=255), primary_key=True),
            sa.Column("grimmory_filename", sa.String(length=500), nullable=False),
            sa.Column("last_scan_at", sa.DateTime(), nullable=False),
            sa.Column("last_top_score", sa.Float(), nullable=True),
            sa.Column("last_status", sa.String(length=50), nullable=True),
        )

    inspector = sa.inspect(bind)
    shelf_indexes = _get_indexes(inspector, "shelf_watch_scans")
    if "ix_shelf_watch_scans_last_scan_at" not in shelf_indexes:
        op.create_index("ix_shelf_watch_scans_last_scan_at", "shelf_watch_scans", ["last_scan_at"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    shelf_indexes = _get_indexes(inspector, "shelf_watch_scans")
    if "ix_shelf_watch_scans_last_scan_at" in shelf_indexes:
        op.drop_index("ix_shelf_watch_scans_last_scan_at", table_name="shelf_watch_scans")
    if "shelf_watch_scans" in inspector.get_table_names():
        op.drop_table("shelf_watch_scans")

    inspector = sa.inspect(bind)
    pending_indexes = _get_indexes(inspector, "pending_suggestions")
    if "ix_pending_suggestions_origin" in pending_indexes:
        op.drop_index("ix_pending_suggestions_origin", table_name="pending_suggestions")
    cols = _get_columns(inspector, "pending_suggestions")
    if "origin_metadata_json" in cols:
        op.drop_column("pending_suggestions", "origin_metadata_json")
    if "origin" in cols:
        op.drop_column("pending_suggestions", "origin")
