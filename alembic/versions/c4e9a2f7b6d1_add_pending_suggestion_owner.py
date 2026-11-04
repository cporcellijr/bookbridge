"""add pending_suggestions.user_id owner

Suggestions become per-reader. Existing rows are backfilled to the default user
(first admin by id, else first user) so nothing disappears for the admin.

Revision ID: c4e9a2f7b6d1
Revises: b8f3d6a1c9e4
Create Date: 2026-11-03
"""

from alembic import op
import sqlalchemy as sa


revision = "c4e9a2f7b6d1"
down_revision = "b8f3d6a1c9e4"
branch_labels = None
depends_on = None

INDEX = "ix_pending_suggestions_user_source"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "pending_suggestions" not in inspector.get_table_names():
        return
    if "user_id" not in {c["name"] for c in inspector.get_columns("pending_suggestions")}:
        with op.batch_alter_table("pending_suggestions") as batch_op:
            batch_op.add_column(sa.Column("user_id", sa.Integer(), nullable=True))
    if INDEX not in {i["name"] for i in sa.inspect(bind).get_indexes("pending_suggestions")}:
        op.create_index(INDEX, "pending_suggestions", ["user_id", "source_id"])
    if "users" in inspector.get_table_names():
        default_id = bind.execute(sa.text(
            "SELECT id FROM users ORDER BY CASE WHEN role = 'admin' THEN 0 ELSE 1 END, id LIMIT 1"
        )).scalar()
        if default_id is not None:
            bind.execute(
                sa.text("UPDATE pending_suggestions SET user_id = :uid WHERE user_id IS NULL"),
                {"uid": default_id},
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "pending_suggestions" not in inspector.get_table_names():
        return
    if INDEX in {i["name"] for i in inspector.get_indexes("pending_suggestions")}:
        op.drop_index(INDEX, table_name="pending_suggestions")
    if "user_id" in {c["name"] for c in inspector.get_columns("pending_suggestions")}:
        with op.batch_alter_table("pending_suggestions") as batch_op:
            batch_op.drop_column("user_id")
