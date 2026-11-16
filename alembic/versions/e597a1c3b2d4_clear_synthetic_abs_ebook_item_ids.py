"""Clear bridge-minted keys stored as an ABS ebook item id (#597).

Match merges copied an ebook-only row's synthetic key (`ebook-<hash>`) into
`abs_ebook_item_id`; no such ABS item exists, so ebook writes 404'd and the book
was marked 'error'. Data-only and idempotent.
"""

from alembic import op

revision = "e597a1c3b2d4"
down_revision = "d7e4a9b2c681"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "UPDATE books SET abs_ebook_item_id = NULL WHERE "
        "lower(trim(abs_ebook_item_id)) LIKE 'ebook-%' "
        "OR lower(trim(abs_ebook_item_id)) LIKE 'ebook:%' "
        "OR lower(trim(abs_ebook_item_id)) LIKE 'booklore:%' "
        "OR lower(trim(abs_ebook_item_id)) LIKE 'bookorbit:%'"
    )


def downgrade():
    pass
