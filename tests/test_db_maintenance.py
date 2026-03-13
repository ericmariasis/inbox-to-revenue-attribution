import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from app.db.maintenance import (
    PostgresToolInvocation,
    build_pg_dump_invocation,
    build_pg_restore_invocation,
    run_pg_dump,
    run_pg_restore,
)


def test_build_pg_dump_invocation_uses_pg_dump_and_pgpassword():
    invocation = build_pg_dump_invocation(
        database_url="postgresql+psycopg://story75_user:story75_pass@db.example.com:5432/story75",
        output_path=Path("tmp/story75.dump"),
    )

    assert invocation.command == (
        "pg_dump",
        "--format=custom",
        "--no-owner",
        "--no-privileges",
        "--file",
        str(Path("tmp/story75.dump")),
        "postgresql://story75_user@db.example.com:5432/story75",
    )
    assert invocation.env_overrides == {"PGPASSWORD": "story75_pass"}


def test_build_pg_restore_invocation_uses_pg_restore_and_pgpassword():
    invocation = build_pg_restore_invocation(
        database_url="postgres://story75_user:story75_pass@db.example.com:5432/story75_restore",
        input_path=Path("tmp/story75.dump"),
    )

    assert invocation.command == (
        "pg_restore",
        "--clean",
        "--if-exists",
        "--no-owner",
        "--no-privileges",
        "--exit-on-error",
        "--dbname",
        "postgresql://story75_user@db.example.com:5432/story75_restore",
        str(Path("tmp/story75.dump")),
    )
    assert invocation.env_overrides == {"PGPASSWORD": "story75_pass"}


def test_run_pg_dump_creates_parent_directory_and_runs_tool(tmp_path: Path):
    output_path = tmp_path / "nested" / "story75.dump"

    with patch("app.db.maintenance.subprocess.run") as mock_run:
        result = run_pg_dump(
            database_url="postgresql://story75_user:story75_pass@db.example.com:5432/story75",
            output_path=output_path,
        )

    assert result == output_path
    assert output_path.parent.is_dir()
    mock_run.assert_called_once()
    called_command = mock_run.call_args.args[0]
    called_env = mock_run.call_args.kwargs["env"]
    assert called_command[0] == "pg_dump"
    assert called_env["PGPASSWORD"] == "story75_pass"


def test_run_pg_restore_requires_existing_input_file(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match="Backup file does not exist"):
        run_pg_restore(
            database_url="postgresql://story75_user:story75_pass@db.example.com:5432/story75_restore",
            input_path=tmp_path / "missing.dump",
        )


def test_run_pg_restore_runs_tool_for_existing_backup_file(tmp_path: Path):
    input_path = tmp_path / "story75.dump"
    input_path.write_bytes(b"story75")

    with patch("app.db.maintenance.subprocess.run") as mock_run:
        result = run_pg_restore(
            database_url="postgresql://story75_user:story75_pass@db.example.com:5432/story75_restore",
            input_path=input_path,
        )

    assert result == input_path
    mock_run.assert_called_once()
    called_command = mock_run.call_args.args[0]
    called_env = mock_run.call_args.kwargs["env"]
    assert called_command[0] == "pg_restore"
    assert called_env["PGPASSWORD"] == "story75_pass"


def test_postgres_tool_invocation_is_subprocess_compatible():
    invocation = PostgresToolInvocation(command=("pg_dump", "--version"), env_overrides={})
    assert isinstance(list(invocation.command), list)


def test_run_pg_dump_propagates_subprocess_failures(tmp_path: Path):
    output_path = tmp_path / "story75.dump"

    with patch(
        "app.db.maintenance.subprocess.run",
        side_effect=subprocess.CalledProcessError(returncode=1, cmd=["pg_dump"]),
    ):
        with pytest.raises(subprocess.CalledProcessError):
            run_pg_dump(
                database_url="postgresql://story75_user:story75_pass@db.example.com:5432/story75",
                output_path=output_path,
            )
