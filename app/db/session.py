import os
from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings


def _database_url() -> str:
    # Prefer explicit test URL when present so integration tests hit migrated test DB.
    return os.getenv("TEST_DATABASE_URL") or get_settings().database_url


ENGINE = create_engine(_database_url())
SessionLocal = sessionmaker(bind=ENGINE, autoflush=False, autocommit=False, class_=Session)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
