from sqlalchemy import create_engine, text

from app.core.config import get_settings
from app.db.url import normalize_database_url


class StartupSmokeError(RuntimeError):
    pass


def run_startup_smoke() -> None:
    settings = get_settings()
    settings.validate_runtime()

    engine = create_engine(normalize_database_url(settings.database_url))
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:
        raise StartupSmokeError("startup smoke failed while connecting to the configured database") from exc
    finally:
        engine.dispose()
