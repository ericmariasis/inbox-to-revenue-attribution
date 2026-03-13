import pytest

from app.db.url import normalize_database_url


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
