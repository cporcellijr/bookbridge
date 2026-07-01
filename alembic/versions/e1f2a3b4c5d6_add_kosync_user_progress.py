"""add kosync_user_progress (per-user device progress for a document hash)

KosyncDocument is keyed by document_hash alone, mixing the SHARED hash cache +
hash->book link with PER-USER device progress, so two users reading the same
EPUB (identical md5) overwrite each other on PUT. This lifts the per-user
progress into its own table keyed by (document_hash, user_id), leaving the
KosyncDocument PK (and its ~30 cache/link callers) untouched. Existing progress
is backfilled to its stamped user (or the default admin) so nobody loses a place.

Revision ID: e1f2a3b4c5d6
Revises: d7f0a2c4e6b8
Create Date: 2026-06-26
"""

from alembic import op
import sqlalchemy as sa


revision = "e1f2a3b4c5d6"
down_revision = "d7f0a2c4e6b8"
branch_labels = None
depends_on = None


def _tables(inspector) -> set:
    return set(inspector.get_table_names())


def _indexes(inspector, table_name: str) -> set:
    if table_name not in inspector.get_table_names():
        return set()
    return {idx["name"] for idx in inspector.get_indexes(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "kosync_user_progress" not in _tables(inspector):
        op.create_table(
            "kosync_user_progress",
            sa.Column("document_hash", sa.String(length=32), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
            sa.Column("progress", sa.String(length=512), nullable=True),
            sa.Column("percentage", sa.Numeric(10, 6), nullable=True),
            sa.Column("device", sa.String(length=128), nullable=True),
            sa.Column("device_id", sa.String(length=64), nullable=True),
            sa.Column("timestamp", sa.DateTime(), nullable=True),
            sa.Column("last_updated", sa.DateTime(), nullable=True),
        )

    inspector = sa.inspect(bind)
    if "ix_kosync_user_progress_user_id" not in _indexes(inspector, "kosync_user_progress"):
        op.create_index("ix_kosync_user_progress_user_id", "kosync_user_progress", ["user_id"])

    # Backfill: copy each existing KosyncDocument's progress to its stamped user
    # (or, when NULL, the default admin / first user). The COALESCE in the WHERE
    # clause filters out every row when no users exist, so the composite PK never
    # takes a NULL user_id. INSERT OR IGNORE keeps it idempotent on re-run.
    tables = _tables(inspector)
    if "kosync_documents" in tables and "users" in tables:
        bind.execute(sa.text(
            "INSERT OR IGNORE INTO kosync_user_progress "
            "(document_hash, user_id, progress, percentage, device, device_id, timestamp, last_updated) "
            "SELECT kd.document_hash, "
            "       COALESCE(kd.user_id, "
            "                (SELECT id FROM users WHERE role='admin' ORDER BY id LIMIT 1), "
            "                (SELECT id FROM users ORDER BY id LIMIT 1)), "
            "       kd.progress, kd.percentage, kd.device, kd.device_id, kd.timestamp, kd.last_updated "
            "FROM kosync_documents kd "
            "WHERE ((kd.percentage IS NOT NULL AND kd.percentage > 0) "
            "       OR (kd.progress IS NOT NULL AND kd.progress != '')) "
            "  AND COALESCE(kd.user_id, "
            "               (SELECT id FROM users WHERE role='admin' ORDER BY id LIMIT 1), "
            "               (SELECT id FROM users ORDER BY id LIMIT 1)) IS NOT NULL"
        ))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "kosync_user_progress" in _tables(inspector):
        op.drop_table("kosync_user_progress")
