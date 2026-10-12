"""merge goodreads details with users autoincrement

Revision ID: 5d2b8e1f4c7a
Revises: a4f7c8d2e901, 8ef64bf8c29e
Create Date: 2026-10-12
"""

from typing import Sequence, Union


# revision identifiers, used by Alembic.
revision: str = "5d2b8e1f4c7a"
down_revision: Union[str, Sequence[str], None] = ("a4f7c8d2e901", "8ef64bf8c29e")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
