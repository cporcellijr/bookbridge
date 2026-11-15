"""Pin explicitly selected provider ebook files; leave legacy mappings unchanged."""

from alembic import op
import sqlalchemy as sa

revision = "d7e4a9b2c681"
down_revision = "c4e7a2b9d1f3"
branch_labels = None
depends_on = None


def upgrade():
    if "ebook_source_file_id" not in {column["name"] for column in sa.inspect(op.get_bind()).get_columns("books")}:
        op.add_column("books", sa.Column("ebook_source_file_id", sa.String(255), nullable=True))


def downgrade():
    if "ebook_source_file_id" in {column["name"] for column in sa.inspect(op.get_bind()).get_columns("books")}:
        op.drop_column("books", "ebook_source_file_id")
