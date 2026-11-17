"""Add covering per-user indexes to koreader_page_stats.

Measured live (92,774 rows on the 9p /data mount): the Library hero's
distinct-md5 and all-start-times reads took 250-290 ms each through the plain
user_id index; covering indexes take them to 40-70 ms.
"""
from alembic import op
import sqlalchemy as sa

revision = 'a7d3e9c2b1f4'
down_revision = 'f1a9c3d8b072'
branch_labels = None
depends_on = None

_INDEXES = {
    'ix_koreader_page_stats_user_md5_start_time': ['user_id', 'md5', 'start_time'],
    'ix_koreader_page_stats_user_start_time': ['user_id', 'start_time'],
}


def _existing(bind) -> set:
    inspector = sa.inspect(bind)
    if not inspector.has_table('koreader_page_stats'):
        return set()
    return {index['name'] for index in inspector.get_indexes('koreader_page_stats')}


def upgrade():
    bind = op.get_bind()
    if not sa.inspect(bind).has_table('koreader_page_stats'):
        return
    existing = _existing(bind)
    for name, columns in _INDEXES.items():
        if name not in existing:
            op.create_index(name, 'koreader_page_stats', columns)


def downgrade():
    existing = _existing(op.get_bind())
    for name in _INDEXES:
        if name in existing:
            op.drop_index(name, table_name='koreader_page_stats')
