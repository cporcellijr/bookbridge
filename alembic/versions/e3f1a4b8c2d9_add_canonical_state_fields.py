"""add canonical state fields

Revision ID: e3f1a4b8c2d9
Revises: b9d4f1c2a6e7
Create Date: 2026-03-17
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e3f1a4b8c2d9"
down_revision: Union[str, Sequence[str], None] = "b9d4f1c2a6e7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {c["name"] for c in inspector.get_columns(table_name)}
    if column.name not in columns:
        op.add_column(table_name, column)


def _drop_column_if_present(table_name: str, column_name: str) -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {c["name"] for c in inspector.get_columns(table_name)}
    if column_name in columns:
        op.drop_column(table_name, column_name)


def upgrade() -> None:
    _add_column_if_missing("states", sa.Column("canonical_text_offset", sa.Integer(), nullable=True))
    _add_column_if_missing("states", sa.Column("canonical_audio_ms", sa.Integer(), nullable=True))
    _add_column_if_missing("states", sa.Column("variant_id", sa.String(length=500), nullable=True))
    _add_column_if_missing("states", sa.Column("mapping_confidence", sa.Float(), nullable=True))
    _add_column_if_missing(
        "states",
        sa.Column("locator_version", sa.Integer(), nullable=False, server_default=sa.text("2")),
    )


def downgrade() -> None:
    _drop_column_if_present("states", "locator_version")
    _drop_column_if_present("states", "mapping_confidence")
    _drop_column_if_present("states", "variant_id")
    _drop_column_if_present("states", "canonical_audio_ms")
    _drop_column_if_present("states", "canonical_text_offset")
