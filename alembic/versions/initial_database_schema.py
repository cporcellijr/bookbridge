"""Initial database schema

Revision ID: 76886bc89d6e
Revises:
Create Date: 2026-01-16 09:39:39.867556

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '76886bc89d6e'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    """Create initial database schema with all tables."""
    # Create books table
    op.create_table(
        'books',
        sa.Column('abs_id', sa.String(255), primary_key=True),
        sa.Column('abs_title', sa.String(500), nullable=True),
        sa.Column('ebook_filename', sa.String(500), nullable=True),
        sa.Column('kosync_doc_id', sa.String(255), nullable=True),
        sa.Column('transcript_file', sa.String(500), nullable=True),
        sa.Column('status', sa.String(50), nullable=True, default='active'),
        sa.Column('duration', sa.Float(), nullable=True),
    )

    # Create hardcover_details table with CASCADE foreign key
    op.create_table(
        'hardcover_details',
        sa.Column('abs_id', sa.String(255), primary_key=True),
        sa.Column('hardcover_book_id', sa.String(255), nullable=True),
        sa.Column('hardcover_edition_id', sa.String(255), nullable=True),
        sa.Column('hardcover_pages', sa.Integer(), nullable=True),
        sa.Column('isbn', sa.String(255), nullable=True),
        sa.Column('asin', sa.String(255), nullable=True),
        sa.Column('matched_by', sa.String(50), nullable=True),
        sa.ForeignKeyConstraint(['abs_id'], ['books.abs_id'], ondelete='CASCADE'),
    )

    # Create storygraph_details table with CASCADE foreign key
    op.create_table(
        'storygraph_details',
        sa.Column('abs_id', sa.String(255), primary_key=True),
        sa.Column('storygraph_book_id', sa.String(255), nullable=True),
        sa.Column('storygraph_url', sa.String(1000), nullable=True),
        sa.Column('storygraph_edition_id', sa.String(255), nullable=True),
        sa.Column('storygraph_pages', sa.Integer(), nullable=True),
        sa.Column('isbn', sa.String(255), nullable=True),
        sa.Column('asin', sa.String(255), nullable=True),
        sa.Column('matched_by', sa.String(50), nullable=True),
        sa.ForeignKeyConstraint(['abs_id'], ['books.abs_id'], ondelete='CASCADE'),
    )

    # Create states table
    op.create_table(
        'states',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('abs_id', sa.String(255), nullable=False),
        sa.Column('client_name', sa.String(50), nullable=False),
        sa.Column('last_updated', sa.Float(), nullable=True),
        sa.Column('percentage', sa.Float(), nullable=True),
        sa.Column('timestamp', sa.Float(), nullable=True),
        sa.Column('xpath', sa.Text(), nullable=True),
        sa.Column('cfi', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['abs_id'], ['books.abs_id'], ondelete='CASCADE'),
    )

    # Create jobs table
    op.create_table(
        'jobs',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('abs_id', sa.String(255), nullable=False),
        sa.Column('last_attempt', sa.Float(), nullable=True),
        sa.Column('retry_count', sa.Integer(), nullable=True, default=0),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['abs_id'], ['books.abs_id'], ondelete='CASCADE'),
    )


def downgrade() -> None:
    """Drop all tables."""
    op.drop_table('jobs')
    op.drop_table('states')
    op.drop_table('hardcover_details')
    op.drop_table('books')
