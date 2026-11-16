"""Add user-scoped BookOrbit session receipts and durable delivery.

Revision ID: e9b5c7a1d023
Revises: e597a1c3b2d4
"""
from alembic import op
import sqlalchemy as sa

revision = 'e9b5c7a1d023'
down_revision = 'e597a1c3b2d4'
branch_labels = None
depends_on = None


def upgrade():
    # A targeted stable hotfix may already have created this additive table.
    # Do not stamp past intervening migrations or recreate its durable receipts.
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table('bookorbit_session_events'):
        required = {'event_id', 'account_scope', 'user_id', 'abs_id', 'bookorbit_book_id',
                    'direction', 'origin', 'status', 'payload_json', 'local_session_id', 'created_at'}
        columns = {column['name'] for column in inspector.get_columns('bookorbit_session_events')}
        if not required <= columns:
            raise RuntimeError('Existing BookOrbit session receipt table has an incompatible schema')
        return
    op.create_table('bookorbit_session_events',
        sa.Column('event_id', sa.String(64), primary_key=True),
        sa.Column('account_scope', sa.String(64), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('abs_id', sa.String(255), nullable=False),
        sa.Column('bookorbit_book_id', sa.Integer(), nullable=False),
        sa.Column('direction', sa.String(20), nullable=False),
        sa.Column('origin', sa.String(32), nullable=False),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('payload_json', sa.Text(), nullable=False),
        sa.Column('local_session_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.Float(), nullable=False),
    )
    op.create_index('ix_bookorbit_session_events_account_scope', 'bookorbit_session_events', ['account_scope'])
    op.create_index('ix_bookorbit_session_events_user_id', 'bookorbit_session_events', ['user_id'])


def downgrade():
    op.drop_table('bookorbit_session_events')
