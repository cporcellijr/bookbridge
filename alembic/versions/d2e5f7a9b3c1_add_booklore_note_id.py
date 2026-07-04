"""add BookLore reader-note (book_notes_v2) spoke id

Revision ID: d2e5f7a9b3c1
Revises: c1f4a8d2e6b9
Create Date: 2026-07-04
"""

from alembic import op
import sqlalchemy as sa


revision = "d2e5f7a9b3c1"
down_revision = "c1f4a8d2e6b9"
branch_labels = None
depends_on = None


def _columns(inspector, table_name: str) -> set:
    if table_name not in inspector.get_table_names():
        return set()
    return {col["name"] for col in inspector.get_columns(table_name)}


def _indexes(inspector, table_name: str) -> set:
    if table_name not in inspector.get_table_names():
        return set()
    return {idx["name"] for idx in inspector.get_indexes(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = _columns(inspector, "koreader_annotations")

    if "booklore_note_id" not in existing:
        op.add_column(
            "koreader_annotations",
            sa.Column("booklore_note_id", sa.Integer(), nullable=True),
        )

    inspector = sa.inspect(bind)
    if "ix_koreader_annotations_booklore_note_id" not in _indexes(inspector, "koreader_annotations"):
        op.create_index(
            "ix_koreader_annotations_booklore_note_id",
            "koreader_annotations",
            ["booklore_note_id"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "ix_koreader_annotations_booklore_note_id" in _indexes(inspector, "koreader_annotations"):
        op.drop_index("ix_koreader_annotations_booklore_note_id", table_name="koreader_annotations")

    if "booklore_note_id" in _columns(sa.inspect(bind), "koreader_annotations"):
        op.drop_column("koreader_annotations", "booklore_note_id")
