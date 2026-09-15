"""add quality_score and quality_detail to book_alignments

Revision ID: 8e5b7a15f647
Revises: f4b8c2d6e9a3
Create Date: 2026-09-09
"""

from alembic import op
import sqlalchemy as sa


revision = "8e5b7a15f647"
down_revision = "f4b8c2d6e9a3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Persist `map_quality.score_map()`'s verdict alongside the map it scored.

    Previously a map's quality was computed and thrown away: a gate rejection or
    publish veto was visible only as a log line, never in the DB, the job row, or
    the UI (issue #426 phase 4). Nullable: existing rows predate scoring and are
    backfilled lazily (`DatabaseService.backfill_alignment_quality`) rather than
    all at once, since map blobs run 10-15MB each.
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "book_alignments" not in set(inspector.get_table_names()):
        return

    columns = {column["name"] for column in inspector.get_columns("book_alignments")}
    with op.batch_alter_table("book_alignments", schema=None) as batch_op:
        if "quality_score" not in columns:
            batch_op.add_column(sa.Column("quality_score", sa.Float(), nullable=True))
        if "quality_detail" not in columns:
            batch_op.add_column(sa.Column("quality_detail", sa.Text(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "book_alignments" not in set(inspector.get_table_names()):
        return

    columns = {column["name"] for column in inspector.get_columns("book_alignments")}
    with op.batch_alter_table("book_alignments", schema=None) as batch_op:
        if "quality_detail" in columns:
            batch_op.drop_column("quality_detail")
        if "quality_score" in columns:
            batch_op.drop_column("quality_score")
