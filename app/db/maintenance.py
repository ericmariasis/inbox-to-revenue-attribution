import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.engine import make_url

from app.db.url import to_postgres_cli_database_url


@dataclass(frozen=True)
class PostgresToolInvocation:
    command: tuple[str, ...]
    env_overrides: dict[str, str]


def build_pg_dump_invocation(*, database_url: str, output_path: str | Path) -> PostgresToolInvocation:
    output = Path(output_path)
    cli_database_url, env_overrides = _build_cli_database_url(database_url)
    return PostgresToolInvocation(
        command=(
            "pg_dump",
            "--format=custom",
            "--no-owner",
            "--no-privileges",
            "--file",
            str(output),
            cli_database_url,
        ),
        env_overrides=env_overrides,
    )


def build_pg_restore_invocation(*, database_url: str, input_path: str | Path) -> PostgresToolInvocation:
    cli_database_url, env_overrides = _build_cli_database_url(database_url)
    return PostgresToolInvocation(
        command=(
            "pg_restore",
            "--clean",
            "--if-exists",
            "--no-owner",
            "--no-privileges",
            "--exit-on-error",
            "--dbname",
            cli_database_url,
            str(Path(input_path)),
        ),
        env_overrides=env_overrides,
    )


def run_pg_dump(*, database_url: str, output_path: str | Path) -> Path:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    _run_postgres_tool(build_pg_dump_invocation(database_url=database_url, output_path=output))
    return output


def run_pg_restore(*, database_url: str, input_path: str | Path) -> Path:
    input_file = Path(input_path)
    if not input_file.is_file():
        raise FileNotFoundError(f"Backup file does not exist: {input_file}")
    _run_postgres_tool(build_pg_restore_invocation(database_url=database_url, input_path=input_file))
    return input_file


def _build_cli_database_url(database_url: str) -> tuple[str, dict[str, str]]:
    parsed = make_url(to_postgres_cli_database_url(database_url))
    password = parsed.password
    cli_database_url = parsed.render_as_string(hide_password=True).replace(":***@", "@")
    env_overrides: dict[str, str] = {}
    if password:
        env_overrides["PGPASSWORD"] = password
    return cli_database_url, env_overrides


def _run_postgres_tool(invocation: PostgresToolInvocation) -> None:
    env = os.environ.copy()
    env.update(invocation.env_overrides)
    subprocess.run(list(invocation.command), check=True, env=env)
