"""add per-user BookFusion book links

Revision ID: b5f3c9d8e1a2
Revises: aa7d9c1e5b2f
Create Date: 2026-07-09
"""

from alembic import op
import sqlalchemy as sa


revision = "b5f3c9d8e1a2"
down_revision = "aa7d9c1e5b2f"
branch_labels = None
depends_on = None


def _tables(inspector) -> set:
    return set(inspector.get_table_names())


def _indexes(inspector, table_name: str) -> set:
    if table_name not in _tables(inspector):
        return set()
    return {idx["name"] for idx in inspector.get_indexes(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "user_bookfusion_links" not in _tables(inspector):
        op.create_table(
            "user_bookfusion_links",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("abs_id", sa.String(255), nullable=False),
            sa.Column("bookfusion_id", sa.String(255), nullable=False),
            sa.Column("title", sa.String(500), nullable=True),
            sa.Column("author", sa.String(500), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["abs_id"], ["books.abs_id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )

    inspector = sa.inspect(bind)
    indexes = _indexes(inspector, "user_bookfusion_links")
    if "ix_user_bookfusion_links_user_id" not in indexes:
        op.create_index("ix_user_bookfusion_links_user_id", "user_bookfusion_links", ["user_id"])
    if "ix_user_bookfusion_links_abs_id" not in indexes:
        op.create_index("ix_user_bookfusion_links_abs_id", "user_bookfusion_links", ["abs_id"])
    if "ix_user_bookfusion_links_user_abs" not in indexes:
        op.create_index(
            "ix_user_bookfusion_links_user_abs",
            "user_bookfusion_links",
            ["user_id", "abs_id"],
            unique=True,
        )
    if "ix_user_bookfusion_links_user_bookfusion" not in indexes:
        op.create_index(
            "ix_user_bookfusion_links_user_bookfusion",
            "user_bookfusion_links",
            ["user_id", "bookfusion_id"],
            unique=True,
        )

    inspector = sa.inspect(bind)
    if "bookfusion_id" in {col["name"] for col in inspector.get_columns("books")}:
        op.execute(
            """
            INSERT OR IGNORE INTO user_bookfusion_links
                (user_id, abs_id, bookfusion_id, title, author, created_at, updated_at)
            SELECT
                ub.user_id,
                b.abs_id,
                b.bookfusion_id,
                b.abs_title,
                NULL,
                CURRENT_TIMESTAMP,
                CURRENT_TIMESTAMP
            FROM books b
            JOIN user_books ub ON ub.abs_id = b.abs_id
            WHERE b.bookfusion_id IS NOT NULL
              AND b.bookfusion_id != ''
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "user_bookfusion_links" in _tables(inspector):
        op.drop_table("user_bookfusion_links")
