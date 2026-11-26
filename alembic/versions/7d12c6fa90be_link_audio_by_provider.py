"""Allow an audio link for each additional provider."""

from alembic import op
import sqlalchemy as sa

revision = '7d12c6fa90be'
down_revision = '6a84c5e91b27'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'user_companion_audio_new',
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('abs_id', sa.String(255), sa.ForeignKey('books.abs_id', ondelete='CASCADE'), primary_key=True),
        sa.Column('provider', sa.String(32), primary_key=True),
        sa.Column('item_id', sa.String(255), nullable=False),
        sa.Column('primary_provider', sa.String(32), nullable=False),
        sa.Column('primary_item_id', sa.String(255), nullable=False),
    )
    op.execute("""INSERT INTO user_companion_audio_new
        SELECT user_id, abs_id, provider, item_id,
               CASE provider WHEN 'ABS' THEN 'BookOrbit' ELSE 'ABS' END, primary_item_id
        FROM user_companion_audio""")
    op.drop_table('user_companion_audio')
    op.rename_table('user_companion_audio_new', 'user_companion_audio')
    op.create_index('ix_user_companion_audio_item', 'user_companion_audio', ['user_id', 'provider', 'item_id'], unique=True)


def downgrade():
    if op.get_bind().execute(sa.text("""SELECT 1 FROM user_companion_audio
        GROUP BY user_id, abs_id HAVING COUNT(*) > 1 OR
        SUM(CASE WHEN (provider='ABS' AND primary_provider='BookOrbit') OR
                      (provider='BookOrbit' AND primary_provider='ABS') THEN 0 ELSE 1 END) > 0
        LIMIT 1""")).first():
        raise RuntimeError('Unlink audio sources not supported by the old schema before downgrading')
    op.create_table(
        'user_companion_audio_old',
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('abs_id', sa.String(255), sa.ForeignKey('books.abs_id', ondelete='CASCADE'), primary_key=True),
        sa.Column('provider', sa.String(32), nullable=False),
        sa.Column('item_id', sa.String(255), nullable=False),
        sa.Column('primary_item_id', sa.String(255), nullable=False),
    )
    op.execute('INSERT INTO user_companion_audio_old SELECT user_id, abs_id, provider, item_id, primary_item_id FROM user_companion_audio')
    op.drop_table('user_companion_audio')
    op.rename_table('user_companion_audio_old', 'user_companion_audio')
    op.create_index('ix_user_companion_audio_item', 'user_companion_audio', ['user_id', 'provider', 'item_id'], unique=True)
