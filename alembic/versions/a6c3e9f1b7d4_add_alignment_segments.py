"""add segments_json to book_alignments and book_alignment_backups

Revision ID: a6c3e9f1b7d4
Revises: 8e5b7a15f647
Create Date: 2026-09-10
"""

from alembic import op
import sqlalchemy as sa


revision = "a6c3e9f1b7d4"
down_revision = "8e5b7a15f647"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the per-segment placement index for out-of-order narration (issue #426
    phase 1). Additive and nullable, no backfill: `alignment_map_json` keeps its
    exact current flat shape, and every existing map's `segments_json` stays NULL,
    which is the documented "flat monotonic map, legacy path" — the 372 live maps
    on the primary install are untouched by this migration.

    `book_alignment_backups` gets the identical column: a backup's map and its
    segments must travel together (a restore that mixed one map's flat points
    with a different map's segment boundaries would silently mis-resolve every
    lookup), so the backup table needs the same field the live table does.
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())

    if "book_alignments" in table_names:
        columns = {column["name"] for column in inspector.get_columns("book_alignments")}
        with op.batch_alter_table("book_alignments", schema=None) as batch_op:
            if "segments_json" not in columns:
                batch_op.add_column(sa.Column("segments_json", sa.Text(), nullable=True))

    if "book_alignment_backups" in table_names:
        columns = {column["name"] for column in inspector.get_columns("book_alignment_backups")}
        with op.batch_alter_table("book_alignment_backups", schema=None) as batch_op:
            if "segments_json" not in columns:
                batch_op.add_column(sa.Column("segments_json", sa.Text(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())

    if "book_alignments" in table_names:
        columns = {column["name"] for column in inspector.get_columns("book_alignments")}
        with op.batch_alter_table("book_alignments", schema=None) as batch_op:
            if "segments_json" in columns:
                batch_op.drop_column("segments_json")

    if "book_alignment_backups" in table_names:
        columns = {column["name"] for column in inspector.get_columns("book_alignment_backups")}
        with op.batch_alter_table("book_alignment_backups", schema=None) as batch_op:
            if "segments_json" in columns:
                batch_op.drop_column("segments_json")
