import os
from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.db.url import normalize_database_url


def _database_url() -> str:
    settings = get_settings()
    test_database_url = os.getenv("TEST_DATABASE_URL")
    # Keep test runs pinned to the migrated test DB, but never let non-local
    # environments accidentally route live traffic to TEST_DATABASE_URL.
    if test_database_url and settings.is_local_env():
        return normalize_database_url(test_database_url)
    return normalize_database_url(settings.database_url)


ENGINE = create_engine(_database_url())
SessionLocal = sessionmaker(bind=ENGINE, autoflush=False, autocommit=False, class_=Session)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
