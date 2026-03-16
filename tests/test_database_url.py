import importlib

import pytest

from app.core.config import get_settings
from app.db.url import normalize_database_url, to_postgres_cli_database_url


@pytest.mark.parametrize(
    ("raw_url", "expected_url"),
    [
        (
            "postgresql://user:pass@db.example.com:5432/app",
            "postgresql+psycopg://user:pass@db.example.com:5432/app",
        ),
        (
            "postgres://user:pass@db.example.com:5432/app",
            "postgresql+psycopg://user:pass@db.example.com:5432/app",
        ),
        (
            "postgresql+psycopg://user:pass@db.example.com:5432/app",
            "postgresql+psycopg://user:pass@db.example.com:5432/app",
        ),
        (
            "sqlite:///tmp/test.db",
            "sqlite:///tmp/test.db",
        ),
    ],
)
def test_normalize_database_url_preserves_psycopg_driver_compatibility(
    raw_url: str,
    expected_url: str,
):
    assert normalize_database_url(raw_url) == expected_url


@pytest.mark.parametrize(
    ("raw_url", "expected_url"),
    [
        (
            "postgresql://user:pass@db.example.com:5432/app",
            "postgresql://user:pass@db.example.com:5432/app",
        ),
        (
            "postgres://user:pass@db.example.com:5432/app",
            "postgresql://user:pass@db.example.com:5432/app",
        ),
        (
            "postgresql+psycopg://user:pass@db.example.com:5432/app",
            "postgresql://user:pass@db.example.com:5432/app",
        ),
    ],
)
def test_to_postgres_cli_database_url_returns_libpq_compatible_urls(
    raw_url: str,
    expected_url: str,
):
    assert to_postgres_cli_database_url(raw_url) == expected_url


def test_to_postgres_cli_database_url_rejects_non_postgres_urls():
    with pytest.raises(ValueError, match="require a PostgreSQL database URL"):
        to_postgres_cli_database_url("sqlite:///tmp/test.db")


@pytest.fixture(autouse=True)
def clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_session_database_url_prefers_test_database_url_in_test_env(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", "postgresql://prod:secret@db.example.com:5432/app")
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql://test:secret@db.example.com:5432/app_test")

    session_module = importlib.import_module("app.db.session")

    assert session_module._database_url() == "postgresql+psycopg://test:secret@db.example.com:5432/app_test"


def test_session_database_url_ignores_test_database_url_outside_local_env(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("APP_ENV", "preview")
    monkeypatch.setenv("DATABASE_URL", "postgresql://prod:secret@db.example.com:5432/app")
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql://test:secret@db.example.com:5432/app_test")

    session_module = importlib.import_module("app.db.session")

    assert session_module._database_url() == "postgresql+psycopg://prod:secret@db.example.com:5432/app"
