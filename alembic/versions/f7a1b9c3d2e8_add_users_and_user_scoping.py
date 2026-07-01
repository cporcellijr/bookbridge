"""add users + user_credentials tables and nullable user_id on per-user tables

Multi-user Phase 1 (schema only). The admin bootstrap, credential copy, and
user_id backfill run at app startup (see ConfigLoader/user bootstrap), not here,
so the schema migration stays free of password hashing and app env coupling.

Revision ID: f7a1b9c3d2e8
Revises: d4e8f1a3b6c2
Create Date: 2026-06-16
"""

from alembic import op
import sqlalchemy as sa


revision = "f7a1b9c3d2e8"
down_revision = "d4e8f1a3b6c2"
branch_labels = None
depends_on = None

# Tables that gain a nullable, indexed user_id (per-user progress/stats).
_USER_SCOPED_TABLES = (
    "states",
    "kosync_documents",
    "reading_sessions",
    "koreader_book_stats",
    "koreader_page_stats",
)


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

    # 1. users
    if "users" not in inspector.get_table_names():
        op.create_table(
            "users",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("username", sa.String(length=255), nullable=False),
            sa.Column("password_hash", sa.String(length=255), nullable=True),
            sa.Column("role", sa.String(length=20), nullable=False, server_default="user"),
            sa.Column("active", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("last_login", sa.DateTime(), nullable=True),
        )

    inspector = sa.inspect(bind)
    if "ix_users_username" not in _get_indexes(inspector, "users"):
        op.create_index("ix_users_username", "users", ["username"], unique=True)

    # 2. user_credentials
    if "user_credentials" not in inspector.get_table_names():
        op.create_table(
            "user_credentials",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("key", sa.String(length=255), nullable=False),
            sa.Column("value", sa.Text(), nullable=True),
        )

    inspector = sa.inspect(bind)
    cred_indexes = _get_indexes(inspector, "user_credentials")
    if "ix_user_credentials_user_id" not in cred_indexes:
        op.create_index("ix_user_credentials_user_id", "user_credentials", ["user_id"])
    if "ix_user_credentials_user_key" not in cred_indexes:
        op.create_index("ix_user_credentials_user_key", "user_credentials", ["user_id", "key"], unique=True)

    # 3. nullable user_id on per-user tables (+ index)
    for table in _USER_SCOPED_TABLES:
        inspector = sa.inspect(bind)
        if table not in inspector.get_table_names():
            continue
        if "user_id" not in _get_columns(inspector, table):
            op.add_column(table, sa.Column("user_id", sa.Integer(), nullable=True))
        inspector = sa.inspect(bind)
        idx_name = f"ix_{table}_user_id"
        if idx_name not in _get_indexes(inspector, table):
            op.create_index(idx_name, table, ["user_id"])


def downgrade() -> None:
    bind = op.get_bind()

    for table in _USER_SCOPED_TABLES:
        inspector = sa.inspect(bind)
        idx_name = f"ix_{table}_user_id"
        if idx_name in _get_indexes(inspector, table):
            op.drop_index(idx_name, table_name=table)
        if "user_id" in _get_columns(inspector, table):
            with op.batch_alter_table(table) as batch_op:
                batch_op.drop_column("user_id")

    inspector = sa.inspect(bind)
    if "user_credentials" in inspector.get_table_names():
        op.drop_table("user_credentials")
    if "users" in inspector.get_table_names():
        op.drop_table("users")
