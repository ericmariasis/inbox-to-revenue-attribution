def normalize_database_url(url: str) -> str:
    normalized = url.strip()
    if normalized.startswith("postgres://"):
        return "postgresql+psycopg://" + normalized.removeprefix("postgres://")
    if normalized.startswith("postgresql://"):
        return "postgresql+psycopg://" + normalized.removeprefix("postgresql://")
    return normalized
