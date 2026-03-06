import os

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from app.services.email_stub import clear_magic_link_outbox


@pytest.fixture(scope="session", autouse=True)
def migrated_test_db():
    db_url = os.environ["TEST_DATABASE_URL"]
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(cfg, "head")
    yield


@pytest.fixture(autouse=True)
def isolate_test_data():
    """Keep tests independent by truncating story-2 tables before each test."""
    db_url = os.environ["TEST_DATABASE_URL"]
    engine = create_engine(db_url)

    with engine.begin() as conn:
        existing = set(inspect(conn).get_table_names(schema="public"))
        required = {"creators", "auth_users", "magic_link_tokens"}
        if required.issubset(existing):
            conn.execute(
                text(
                    "TRUNCATE TABLE magic_link_tokens, auth_users, creators "
                    "RESTART IDENTITY CASCADE"
                )
            )


@pytest.fixture(autouse=True)
def clear_email_stub_outbox():
    clear_magic_link_outbox()
