"""add total_pages to koreader_page_stats

Revision ID: d4e8f1a3b6c2
Revises: b7c9d1e3f5a2
Create Date: 2026-06-10
"""

from alembic import op
import sqlalchemy as sa


revision = "d4e8f1a3b6c2"
down_revision = "b7c9d1e3f5a2"
branch_labels = None
depends_on = None


def _get_columns(inspector, table_name: str) -> set[str]:
    if table_name not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    columns = _get_columns(inspector, "koreader_page_stats")
    if columns and "total_pages" not in columns:
        op.add_column("koreader_page_stats", sa.Column("total_pages", sa.Integer(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    columns = _get_columns(inspector, "koreader_page_stats")
    if "total_pages" in columns:
        with op.batch_alter_table("koreader_page_stats") as batch_op:
            batch_op.drop_column("total_pages")
