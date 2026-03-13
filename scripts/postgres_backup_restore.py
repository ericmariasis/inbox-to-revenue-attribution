import argparse
import os
from pathlib import Path

from app.db.maintenance import run_pg_dump, run_pg_restore


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Run the minimum Story 75 logical backup and scratch restore workflow for PostgreSQL.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    backup_parser = subparsers.add_parser(
        "backup",
        help="Capture a custom-format pg_dump backup from the configured PostgreSQL database.",
    )
    backup_parser.add_argument(
        "--database-url",
        default=os.getenv("DATABASE_URL"),
        help="Source PostgreSQL database URL. Defaults to DATABASE_URL.",
    )
    backup_parser.add_argument(
        "--output",
        required=True,
        help="Path to the output .dump file.",
    )

    restore_parser = subparsers.add_parser(
        "restore",
        help="Restore a custom-format backup into a scratch PostgreSQL database.",
    )
    restore_parser.add_argument(
        "--database-url",
        default=os.getenv("DATABASE_URL"),
        help="Target PostgreSQL database URL. Defaults to DATABASE_URL.",
    )
    restore_parser.add_argument(
        "--input",
        required=True,
        help="Path to the input .dump file.",
    )

    args = parser.parse_args(argv)
    database_url = args.database_url
    if not database_url:
        parser.error("DATABASE_URL must be set or --database-url must be provided.")

    if args.command == "backup":
        output = run_pg_dump(database_url=database_url, output_path=Path(args.output))
        print(f"postgres_backup_ok output={output}")
        return

    restored = run_pg_restore(database_url=database_url, input_path=Path(args.input))
    print(f"postgres_restore_ok input={restored}")


if __name__ == "__main__":
    main()
