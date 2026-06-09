"""add embedding cache table

Revision ID: b7c9d1e3f5a2
Revises: a1b2c3d4e5f6
Create Date: 2026-06-09
"""

from alembic import op
import sqlalchemy as sa

revision = "b7c9d1e3f5a2"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def _get_indexes(inspector, table_name: str) -> set:
    if table_name not in inspector.get_table_names():
        return set()
    return {idx["name"] for idx in inspector.get_indexes(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "embedding_cache" not in inspector.get_table_names():
        op.create_table(
            "embedding_cache",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("model", sa.String(length=255), nullable=False),
            sa.Column("text_hash", sa.String(length=64), nullable=False),
            sa.Column("vector_json", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=True),
        )

    inspector = sa.inspect(bind)
    if "ix_embedding_cache_model_hash" not in _get_indexes(inspector, "embedding_cache"):
        op.create_index(
            "ix_embedding_cache_model_hash",
            "embedding_cache",
            ["model", "text_hash"],
            unique=True,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "ix_embedding_cache_model_hash" in _get_indexes(inspector, "embedding_cache"):
        op.drop_index("ix_embedding_cache_model_hash", table_name="embedding_cache")
    if "embedding_cache" in inspector.get_table_names():
        op.drop_table("embedding_cache")
