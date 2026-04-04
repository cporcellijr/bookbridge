"""add koreader statistics tables

Revision ID: c8a2d7e4f1b9
Revises: b3d5f7a9c1e2
Create Date: 2026-04-03
"""

from alembic import op
import sqlalchemy as sa


revision = "c8a2d7e4f1b9"
down_revision = "b3d5f7a9c1e2"
branch_labels = None
depends_on = None


def _get_columns(inspector, table_name: str) -> set[str]:
    if table_name not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(table_name)}


def _get_indexes(inspector, table_name: str) -> set[str]:
    if table_name not in inspector.get_table_names():
        return set()
    return {index["name"] for index in inspector.get_indexes(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "koreader_book_stats" not in inspector.get_table_names():
        op.create_table(
            "koreader_book_stats",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("md5", sa.String(length=32), nullable=False),
            sa.Column("device", sa.String(length=128), nullable=True),
            sa.Column("device_id", sa.String(length=128), nullable=True),
            sa.Column("device_key", sa.String(length=128), nullable=False),
            sa.Column("ko_book_id", sa.Integer(), nullable=True),
            sa.Column("title", sa.String(length=500), nullable=True),
            sa.Column("authors", sa.String(length=500), nullable=True),
            sa.Column("pages", sa.Integer(), nullable=True),
            sa.Column("total_read_pages", sa.Integer(), nullable=True),
            sa.Column("total_read_time", sa.Integer(), nullable=True),
            sa.Column("last_updated", sa.DateTime(), nullable=True),
            sa.UniqueConstraint("md5", "device_key", name="uq_koreader_book_stats_md5_device_key"),
        )
    else:
        columns = _get_columns(inspector, "koreader_book_stats")
        for column in (
            sa.Column("md5", sa.String(length=32), nullable=False),
            sa.Column("device", sa.String(length=128), nullable=True),
            sa.Column("device_id", sa.String(length=128), nullable=True),
            sa.Column("device_key", sa.String(length=128), nullable=False),
            sa.Column("ko_book_id", sa.Integer(), nullable=True),
            sa.Column("title", sa.String(length=500), nullable=True),
            sa.Column("authors", sa.String(length=500), nullable=True),
            sa.Column("pages", sa.Integer(), nullable=True),
            sa.Column("total_read_pages", sa.Integer(), nullable=True),
            sa.Column("total_read_time", sa.Integer(), nullable=True),
            sa.Column("last_updated", sa.DateTime(), nullable=True),
        ):
            if column.name not in columns:
                op.add_column("koreader_book_stats", column)

    indexes = _get_indexes(inspector, "koreader_book_stats")
    if "ix_koreader_book_stats_device_key" not in indexes:
        op.create_index("ix_koreader_book_stats_device_key", "koreader_book_stats", ["device_key"], unique=False)
    if "ix_koreader_book_stats_md5" not in indexes:
        op.create_index("ix_koreader_book_stats_md5", "koreader_book_stats", ["md5"], unique=False)
    if "ix_koreader_book_stats_last_updated" not in indexes:
        op.create_index("ix_koreader_book_stats_last_updated", "koreader_book_stats", ["last_updated"], unique=False)

    inspector = sa.inspect(bind)
    if "koreader_page_stats" not in inspector.get_table_names():
        op.create_table(
            "koreader_page_stats",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("md5", sa.String(length=32), nullable=False),
            sa.Column("device", sa.String(length=128), nullable=True),
            sa.Column("device_id", sa.String(length=128), nullable=True),
            sa.Column("device_key", sa.String(length=128), nullable=False),
            sa.Column("page", sa.Integer(), nullable=False),
            sa.Column("start_time", sa.Float(), nullable=False),
            sa.Column("duration", sa.Float(), nullable=False),
            sa.Column("uploaded_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint("md5", "device_key", "page", "start_time", name="uq_koreader_page_stats_replay"),
        )
    else:
        columns = _get_columns(inspector, "koreader_page_stats")
        for column in (
            sa.Column("md5", sa.String(length=32), nullable=False),
            sa.Column("device", sa.String(length=128), nullable=True),
            sa.Column("device_id", sa.String(length=128), nullable=True),
            sa.Column("device_key", sa.String(length=128), nullable=False),
            sa.Column("page", sa.Integer(), nullable=False),
            sa.Column("start_time", sa.Float(), nullable=False),
            sa.Column("duration", sa.Float(), nullable=False),
            sa.Column("uploaded_at", sa.DateTime(), nullable=True),
        ):
            if column.name not in columns:
                op.add_column("koreader_page_stats", column)

    indexes = _get_indexes(inspector, "koreader_page_stats")
    if "ix_koreader_page_stats_start_time" not in indexes:
        op.create_index("ix_koreader_page_stats_start_time", "koreader_page_stats", ["start_time"], unique=False)
    if "ix_koreader_page_stats_device_key_start_time" not in indexes:
        op.create_index(
            "ix_koreader_page_stats_device_key_start_time",
            "koreader_page_stats",
            ["device_key", "start_time"],
            unique=False,
        )
    if "ix_koreader_page_stats_md5_start_time" not in indexes:
        op.create_index(
            "ix_koreader_page_stats_md5_start_time",
            "koreader_page_stats",
            ["md5", "start_time"],
            unique=False,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "koreader_page_stats" in inspector.get_table_names():
        indexes = _get_indexes(inspector, "koreader_page_stats")
        if "ix_koreader_page_stats_md5_start_time" in indexes:
            op.drop_index("ix_koreader_page_stats_md5_start_time", table_name="koreader_page_stats")
        if "ix_koreader_page_stats_device_key_start_time" in indexes:
            op.drop_index("ix_koreader_page_stats_device_key_start_time", table_name="koreader_page_stats")
        if "ix_koreader_page_stats_start_time" in indexes:
            op.drop_index("ix_koreader_page_stats_start_time", table_name="koreader_page_stats")
        op.drop_table("koreader_page_stats")

    inspector = sa.inspect(bind)
    if "koreader_book_stats" in inspector.get_table_names():
        indexes = _get_indexes(inspector, "koreader_book_stats")
        if "ix_koreader_book_stats_last_updated" in indexes:
            op.drop_index("ix_koreader_book_stats_last_updated", table_name="koreader_book_stats")
        if "ix_koreader_book_stats_md5" in indexes:
            op.drop_index("ix_koreader_book_stats_md5", table_name="koreader_book_stats")
        if "ix_koreader_book_stats_device_key" in indexes:
            op.drop_index("ix_koreader_book_stats_device_key", table_name="koreader_book_stats")
        op.drop_table("koreader_book_stats")
