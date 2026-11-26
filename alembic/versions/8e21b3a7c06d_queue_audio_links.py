"""Persist audio link verification jobs."""
import sqlalchemy as sa
from alembic import op

revision = '8e21b3a7c06d'
down_revision = '7d12c6fa90be'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('jobs', sa.Column('audio_link_user_id', sa.Integer(), nullable=True))
    op.add_column('jobs', sa.Column('audio_link_inputs', sa.JSON(), nullable=True))


def downgrade():
    op.execute("DELETE FROM jobs WHERE kind = 'audio_link'")
    with op.batch_alter_table('jobs') as batch:
        batch.drop_column('audio_link_inputs')
        batch.drop_column('audio_link_user_id')
