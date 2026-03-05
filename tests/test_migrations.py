import os
from alembic import command
from alembic.config import Config

def test_migrations_upgrade_and_downgrade():
    db_url = os.getenv("TEST_DATABASE_URL")
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", db_url)

    command.upgrade(cfg, "head")
    command.downgrade(cfg, "-1")
    command.upgrade(cfg, "head")