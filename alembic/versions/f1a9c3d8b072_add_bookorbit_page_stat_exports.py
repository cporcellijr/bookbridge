"""Add durable native KOReader page-event delivery receipts."""
from alembic import op
import sqlalchemy as sa

revision = 'f1a9c3d8b072'
down_revision = 'e9b5c7a1d023'
branch_labels = None
depends_on = None


def upgrade():
    if sa.inspect(op.get_bind()).has_table('bookorbit_page_stat_exports'):
        return
    op.create_table('bookorbit_page_stat_exports',
        sa.Column('event_id', sa.String(64), primary_key=True),
        sa.Column('account_scope', sa.String(64), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('page_stat_id', sa.Integer(), nullable=False),
        sa.Column('last_attempt_at', sa.Float(), nullable=False, server_default='0'),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('payload_json', sa.Text(), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False),
    )
    for column in ('account_scope', 'user_id', 'page_stat_id'):
        op.create_index(f'ix_bookorbit_page_stat_exports_{column}', 'bookorbit_page_stat_exports', [column])


def downgrade():
    op.drop_table('bookorbit_page_stat_exports')
