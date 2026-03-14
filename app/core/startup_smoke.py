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
    repo_root = _resolve_repo_root()
    config = Config(str(repo_root / "alembic.ini"))
    config.set_main_option("script_location", str(repo_root / "migrations"))
    return ScriptDirectory.from_config(config).get_current_head()


def _resolve_repo_root() -> Path:
    candidate_roots: list[Path] = []
    seen: set[Path] = set()

    for start_path in (Path.cwd().resolve(), Path(__file__).resolve()):
        path_candidates = [start_path, *start_path.parents]
        for candidate in path_candidates:
            if candidate in seen:
                continue
            seen.add(candidate)
            candidate_roots.append(candidate)

    for candidate in candidate_roots:
        if (candidate / "alembic.ini").is_file() and (candidate / "migrations").is_dir():
            return candidate

    raise StartupSmokeError("startup smoke could not locate alembic.ini and migrations from the current project root")
