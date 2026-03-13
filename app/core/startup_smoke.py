from dataclasses import dataclass
from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, create_engine, text

from app.core.config import get_settings
from app.db.url import normalize_database_url


class StartupSmokeError(RuntimeError):
    pass


@dataclass(frozen=True)
class StartupSmokeResult:
    current_revision: str | None
    head_revision: str | None
    schema_ready: bool


def run_startup_smoke(*, require_schema: bool = False) -> StartupSmokeResult:
    settings = get_settings()
    settings.validate_runtime()

    engine = create_engine(normalize_database_url(settings.database_url))
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            result = _load_schema_state(connection)
    except Exception as exc:
        raise StartupSmokeError("startup smoke failed while connecting to the configured database") from exc
    finally:
        engine.dispose()

    if require_schema and not result.schema_ready:
        raise StartupSmokeError(
            "startup smoke connected to the configured database but the schema is not at the current migration head"
        )

    return result


def _load_schema_state(connection: Connection) -> StartupSmokeResult:
    current_revision = MigrationContext.configure(connection).get_current_revision()
    head_revision = _load_migration_head_revision()
    return StartupSmokeResult(
        current_revision=current_revision,
        head_revision=head_revision,
        schema_ready=(current_revision == head_revision and head_revision is not None),
    )


def _load_migration_head_revision() -> str | None:
    repo_root = Path(__file__).resolve().parents[2]
    config = Config(str(repo_root / "alembic.ini"))
    config.set_main_option("script_location", str(repo_root / "migrations"))
    return ScriptDirectory.from_config(config).get_current_head()
