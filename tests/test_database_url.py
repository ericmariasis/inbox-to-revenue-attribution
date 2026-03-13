import pytest

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
