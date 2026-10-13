"""merge goodreads with kosync document hash method

Revision ID: 7c1e4f9a2b6d
Revises: 5d2b8e1f4c7a, a3d9c5e7b142
Create Date: 2026-10-12
"""

from typing import Sequence, Union


# revision identifiers, used by Alembic.
revision: str = "7c1e4f9a2b6d"
down_revision: Union[str, Sequence[str], None] = ("5d2b8e1f4c7a", "a3d9c5e7b142")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
