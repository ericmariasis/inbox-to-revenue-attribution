import os

import pytest
from alembic import command
from alembic.config import Config


@pytest.fixture(scope="session", autouse=True)
def migrated_test_db():
    db_url = os.environ["TEST_DATABASE_URL"]
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(cfg, "head")
    yield
