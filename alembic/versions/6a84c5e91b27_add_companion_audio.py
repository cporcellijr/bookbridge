"""Add per-user companion audiobook links."""

from alembic import op
import sqlalchemy as sa

revision = '6a84c5e91b27'
down_revision = 'd3f1a7c9e2b5'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'user_companion_audio',
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('abs_id', sa.String(255), sa.ForeignKey('books.abs_id', ondelete='CASCADE'), primary_key=True),
        sa.Column('provider', sa.String(32), nullable=False),
        sa.Column('item_id', sa.String(255), nullable=False),
        sa.Column('primary_item_id', sa.String(255), nullable=False),
    )
    op.create_index('ix_user_companion_audio_item', 'user_companion_audio', ['user_id', 'provider', 'item_id'], unique=True)


def downgrade():
    op.drop_table('user_companion_audio')
