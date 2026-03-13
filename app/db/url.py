def normalize_database_url(url: str) -> str:
    normalized = url.strip()
    if normalized.startswith("postgres://"):
        return "postgresql+psycopg://" + normalized.removeprefix("postgres://")
    if normalized.startswith("postgresql://"):
        return "postgresql+psycopg://" + normalized.removeprefix("postgresql://")
    return normalized


def to_postgres_cli_database_url(url: str) -> str:
    normalized = url.strip()
    if normalized.startswith("postgresql+psycopg://"):
        return "postgresql://" + normalized.removeprefix("postgresql+psycopg://")
    if normalized.startswith("postgres://"):
        return "postgresql://" + normalized.removeprefix("postgres://")
    if normalized.startswith("postgresql://"):
        return normalized
    raise ValueError("pg_dump and pg_restore require a PostgreSQL database URL")
