"""add per-user BookOrbit ebook/audio links

Revision ID: c2a4f8d6e1b3
Revises: b5f3c9d8e1a2
Create Date: 2026-07-13
"""

from alembic import op
import sqlalchemy as sa


revision = "c2a4f8d6e1b3"
down_revision = "b5f3c9d8e1a2"
branch_labels = None
depends_on = None


def _tables(inspector) -> set:
    return set(inspector.get_table_names())


def _indexes(inspector, table_name: str) -> set:
    if table_name not in _tables(inspector):
        return set()
    return {idx["name"] for idx in inspector.get_indexes(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "user_bookorbit_links" not in _tables(inspector):
        op.create_table(
            "user_bookorbit_links",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("abs_id", sa.String(255), nullable=False),
            sa.Column("ebook_id", sa.String(255), nullable=True),
            sa.Column("audio_id", sa.String(255), nullable=True),
            sa.Column("title", sa.String(500), nullable=True),
            sa.Column("author", sa.String(500), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["abs_id"], ["books.abs_id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )

    inspector = sa.inspect(bind)
    indexes = _indexes(inspector, "user_bookorbit_links")
    if "ix_user_bookorbit_links_user_id" not in indexes:
        op.create_index("ix_user_bookorbit_links_user_id", "user_bookorbit_links", ["user_id"])
    if "ix_user_bookorbit_links_abs_id" not in indexes:
        op.create_index("ix_user_bookorbit_links_abs_id", "user_bookorbit_links", ["abs_id"])
    if "ix_user_bookorbit_links_user_abs" not in indexes:
        op.create_index(
            "ix_user_bookorbit_links_user_abs",
            "user_bookorbit_links",
            ["user_id", "abs_id"],
            unique=True,
        )

    # Backfill from existing Book rows where ebook_source='BookOrbit' or
    # audio_source='BookOrbit' for the book's creator/known claimant.
    # Only backfill if the columns exist on the books table.
    inspector = sa.inspect(bind)
    book_cols = {col["name"] for col in inspector.get_columns("books")}
    has_ebook_src = "ebook_source" in book_cols
    has_audio_src = "audio_source" in book_cols
    has_ebook_sid = "ebook_source_id" in book_cols
    has_audio_sid = "audio_source_id" in book_cols
    has_user_id = "user_id" in book_cols
    has_audio_provider_id = "audio_provider_book_id" in book_cols
    tables = _tables(inspector)

    if has_ebook_src or has_audio_src:
        # Build the backfill SELECT dynamically based on available columns.
        # Prefer book.user_id (creator); fall back to the first claimant from
        # user_books when the creator is NULL.
        ebook_id_expr = "NULL"
        if has_ebook_src and has_ebook_sid:
            ebook_id_expr = "CASE WHEN b.ebook_source = 'BookOrbit' THEN b.ebook_source_id ELSE NULL END"
        audio_id_expr = "NULL"
        if has_audio_src and (has_audio_provider_id or has_audio_sid):
            audio_columns = []
            if has_audio_provider_id:
                audio_columns.append("b.audio_provider_book_id")
            if has_audio_sid:
                audio_columns.append("b.audio_source_id")
            audio_value_expr = (
                audio_columns[0]
                if len(audio_columns) == 1
                else f"COALESCE({', '.join(audio_columns)})"
            )
            audio_id_expr = (
                "CASE WHEN b.audio_source = 'BookOrbit' "
                f"THEN {audio_value_expr} ELSE NULL END"
            )

        owner_candidates = []
        if has_user_id:
            owner_candidates.append("b.user_id")
        if "user_books" in tables:
            owner_candidates.append(
                "(SELECT ub.user_id FROM user_books ub "
                "WHERE ub.abs_id = b.abs_id ORDER BY ub.user_id LIMIT 1)"
            )
        if "users" in tables:
            owner_candidates.extend([
                "(SELECT u.id FROM users u WHERE u.role = 'admin' ORDER BY u.id LIMIT 1)",
                "(SELECT u.id FROM users u ORDER BY u.id LIMIT 1)",
            ])
        if not owner_candidates:
            return
        user_expr = (
            owner_candidates[0]
            if len(owner_candidates) == 1
            else f"COALESCE({', '.join(owner_candidates)})"
        )

        op.execute(
            f"""
            INSERT OR IGNORE INTO user_bookorbit_links
                (user_id, abs_id, ebook_id, audio_id, title, author, created_at, updated_at)
            SELECT
                {user_expr},
                b.abs_id,
                {ebook_id_expr},
                {audio_id_expr},
                b.abs_title,
                NULL,
                CURRENT_TIMESTAMP,
                CURRENT_TIMESTAMP
            FROM books b
            WHERE ({user_expr}) IS NOT NULL
              AND (({ebook_id_expr}) IS NOT NULL
               OR ({audio_id_expr}) IS NOT NULL)
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "user_bookorbit_links" in _tables(inspector):
        op.drop_table("user_bookorbit_links")
