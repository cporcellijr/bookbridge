"""add align_method to book_alignments

Revision ID: a1b2c3d4e5f6
Revises: f3c8e2a5b9d4
Create Date: 2026-06-09
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "f3c8e2a5b9d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {c["name"] for c in inspector.get_columns("book_alignments")}
    if "align_method" not in columns:
        op.add_column("book_alignments", sa.Column("align_method", sa.String(length=32), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {c["name"] for c in inspector.get_columns("book_alignments")}
    if "align_method" in columns:
        op.drop_column("book_alignments", "align_method")
