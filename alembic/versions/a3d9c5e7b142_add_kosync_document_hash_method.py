"""record which KOSYNC_HASH_METHOD produced each cached kosync hash

The hash cache keyed rows by hash only, so after a method switch it could not
tell a filename-mode hash from a content-mode one. Rows with a cached filename
are backfilled: a hash equal to md5(filename) is 'filename', any other is
'content'. Rows without a filename stay NULL.

Revision ID: a3d9c5e7b142
Revises: 8ef64bf8c29e
Create Date: 2026-10-12
"""

import hashlib
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a3d9c5e7b142"
down_revision: Union[str, Sequence[str], None] = "8ef64bf8c29e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(bind) -> set:
    inspector = sa.inspect(bind)
    if "kosync_documents" not in inspector.get_table_names():
        return set()
    return {c["name"] for c in inspector.get_columns("kosync_documents")}


def upgrade() -> None:
    bind = op.get_bind()
    columns = _columns(bind)
    if not columns or "hash_method" in columns:
        return
    op.add_column("kosync_documents", sa.Column("hash_method", sa.String(16), nullable=True))
    rows = bind.execute(sa.text(
        "SELECT document_hash, filename FROM kosync_documents WHERE filename IS NOT NULL"
    )).fetchall()
    for document_hash, filename in rows:
        filename_hash = hashlib.md5(filename.encode("utf-8"), usedforsecurity=False).hexdigest()
        bind.execute(
            sa.text("UPDATE kosync_documents SET hash_method = :m WHERE document_hash = :h"),
            {"m": "filename" if document_hash == filename_hash else "content", "h": document_hash},
        )


def downgrade() -> None:
    bind = op.get_bind()
    if "hash_method" in _columns(bind):
        with op.batch_alter_table("kosync_documents") as batch_op:
            batch_op.drop_column("hash_method")
