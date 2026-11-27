"""Both previously deployed heads can upgrade without losing their data."""

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config


pytestmark = pytest.mark.real_database_migrations
ROOT = Path(__file__).resolve().parents[1]
PREVIEW = '8e21b3a7c06d'
UPSTREAM = 'k26d20261126'
MERGE = 'a481d26f019e'


@pytest.mark.parametrize('previous', [PREVIEW, UPSTREAM])
def test_upgrade_preserves_existing_branch_data(tmp_path, previous):
    url = f'sqlite:///{tmp_path / "history.db"}'
    cfg = Config(str(ROOT / 'alembic.ini'))
    cfg.set_main_option('script_location', str(ROOT / 'alembic'))
    cfg.set_main_option('sqlalchemy.url', url)
    command.upgrade(cfg, previous)
    engine = sa.create_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(sa.text("INSERT INTO users (id, username, role, active) VALUES (1, 'reader', 'user', 1)"))
            if previous == PREVIEW:
                conn.execute(sa.text("INSERT INTO books (abs_id, abs_title) VALUES ('book', 'Example')"))
                conn.execute(sa.text("""INSERT INTO user_companion_audio
                    (user_id, abs_id, provider, item_id, primary_provider, primary_item_id)
                    VALUES (1, 'book', 'BookOrbit', '42', 'ABS', 'book')"""))
            else:
                conn.execute(sa.text("""INSERT INTO kosync_device_reports
                    (document_hash, user_id, device_id, progress, percentage)
                    VALUES ('0123456789abcdef0123456789abcdef', 1, 'reader-device', '/body/p[1].0', 0.5)"""))

        command.upgrade(cfg, 'head')

        assert {'user_companion_audio', 'kosync_device_reports'} <= set(sa.inspect(engine).get_table_names())
        with engine.connect() as conn:
            assert conn.execute(sa.text('SELECT version_num FROM alembic_version')).scalar_one() == MERGE
            if previous == PREVIEW:
                assert conn.execute(sa.text('SELECT provider, item_id FROM user_companion_audio')).one() == ('BookOrbit', '42')
            else:
                assert conn.execute(sa.text('SELECT device_id, percentage FROM kosync_device_reports')).one() == ('reader-device', 0.5)
    finally:
        engine.dispose()
