"""Add reading_sessions table

Revision ID: b3d5f7a9c1e2
Revises: a7c9e1d3f4b2
Create Date: 2026-03-24
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'b3d5f7a9c1e2'
down_revision = 'a7c9e1d3f4b2'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if 'reading_sessions' in inspector.get_table_names():
        return

    op.create_table(
        'reading_sessions',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('abs_id', sa.String(255), sa.ForeignKey('books.abs_id', ondelete='CASCADE'), nullable=False),
        sa.Column('session_type', sa.String(20), nullable=False),
        sa.Column('start_time', sa.Float(), nullable=False),
        sa.Column('end_time', sa.Float(), nullable=False),
        sa.Column('duration_seconds', sa.Integer(), nullable=False),
        sa.Column('start_progress', sa.Float(), nullable=True),
        sa.Column('end_progress', sa.Float(), nullable=True),
        sa.Column('leader_client', sa.String(50), nullable=True),
    )
    op.create_index('ix_reading_sessions_abs_id', 'reading_sessions', ['abs_id'])


def downgrade():
    op.drop_index('ix_reading_sessions_abs_id', table_name='reading_sessions')
    op.drop_table('reading_sessions')
