"""add rich locator payload fields to states

Revision ID: b9d4f1c2a6e7
Revises: a7c9e1d3f4b2
Create Date: 2026-03-17
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b9d4f1c2a6e7"
down_revision: Union[str, Sequence[str], None] = "a7c9e1d3f4b2"
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
    _add_column_if_missing("states", sa.Column("raw_percentage", sa.Float(), nullable=True))
    _add_column_if_missing("states", sa.Column("locator_json", sa.Text(), nullable=True))


def downgrade() -> None:
    _drop_column_if_present("states", "locator_json")
    _drop_column_if_present("states", "raw_percentage")
