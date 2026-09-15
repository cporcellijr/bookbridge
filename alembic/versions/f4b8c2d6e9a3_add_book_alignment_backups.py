"""add book_alignment_backups table

Revision ID: f4b8c2d6e9a3
Revises: b5d1f0a73c24
Create Date: 2026-09-09
"""

from alembic import op
import sqlalchemy as sa


revision = "f4b8c2d6e9a3"
down_revision = "b5d1f0a73c24"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Keep the map a book had before its last CTC overwrite (issue #426).

    CTC forced alignment replaces a book's map in place. A map can pass the
    acceptance gate yet still be worse in positioning than the one it replaced,
    so the prior map is copied here first — one row per book — making a bad remap
    instantly reversible. Idempotent: skip when the table already exists.
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "book_alignment_backups" in set(inspector.get_table_names()):
        return

    op.create_table(
        "book_alignment_backups",
        sa.Column("abs_id", sa.String(length=255), nullable=False),
        sa.Column("alignment_map_json", sa.Text(), nullable=False),
        sa.Column("align_method", sa.String(length=32), nullable=True),
        sa.Column("total_chars", sa.Integer(), nullable=True),
        sa.Column("backed_up_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["abs_id"], ["books.abs_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("abs_id"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "book_alignment_backups" not in set(inspector.get_table_names()):
        return

    op.drop_table("book_alignment_backups")
