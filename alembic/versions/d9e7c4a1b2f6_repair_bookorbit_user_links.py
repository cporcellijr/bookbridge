"""repair missing per-user BookOrbit links

Revision ID: d9e7c4a1b2f6
Revises: c2a4f8d6e1b3
Create Date: 2026-07-14
"""

from alembic import op
import sqlalchemy as sa


revision = "d9e7c4a1b2f6"
down_revision = "c2a4f8d6e1b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Backfill links skipped when a legacy book had a NULL creator."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if not {"books", "users", "user_bookorbit_links"}.issubset(tables):
        return

    book_cols = {column["name"] for column in inspector.get_columns("books")}
    has_ebook_src = "ebook_source" in book_cols
    has_audio_src = "audio_source" in book_cols
    has_ebook_sid = "ebook_source_id" in book_cols
    has_audio_sid = "audio_source_id" in book_cols
    has_audio_provider_id = "audio_provider_book_id" in book_cols

    ebook_id_expr = "NULL"
    if has_ebook_src and has_ebook_sid:
        ebook_id_expr = (
            "CASE WHEN b.ebook_source = 'BookOrbit' "
            "THEN b.ebook_source_id ELSE NULL END"
        )

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
    if "user_id" in book_cols:
        owner_candidates.append("b.user_id")
    if "user_books" in tables:
        owner_candidates.append(
            "(SELECT ub.user_id FROM user_books ub "
            "WHERE ub.abs_id = b.abs_id ORDER BY ub.user_id LIMIT 1)"
        )
    owner_candidates.extend([
        "(SELECT u.id FROM users u WHERE u.role = 'admin' ORDER BY u.id LIMIT 1)",
        "(SELECT u.id FROM users u ORDER BY u.id LIMIT 1)",
    ])
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
    """Keep repaired ownership rows when downgrading this data migration."""
    pass
