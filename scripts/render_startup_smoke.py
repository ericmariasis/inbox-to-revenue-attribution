import argparse

from app.core.startup_smoke import run_startup_smoke


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Run the narrow Render startup smoke for config, database connectivity, and optional schema readiness.",
    )
    parser.add_argument(
        "--require-schema",
        action="store_true",
        help="Fail if the current database schema is not at the repo migration head.",
    )
    args = parser.parse_args(argv)

    result = run_startup_smoke(require_schema=args.require_schema)
    print(
        "render_startup_smoke_ok "
        f"schema_ready={'true' if result.schema_ready else 'false'} "
        f"current_revision={result.current_revision or 'none'} "
        f"head_revision={result.head_revision or 'none'}"
    )


if __name__ == "__main__":
    main()
