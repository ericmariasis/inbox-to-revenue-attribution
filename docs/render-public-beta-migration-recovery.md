# Render Public-Beta Migration And Recovery

This runbook defines the minimum Story 75 migration-safety and restore-confidence workflow for the first public beta.

It intentionally stays small:

- one manual release checklist for schema-changing deploys
- one logical custom-format `pg_dump` backup before migration releases
- one scratch-database `pg_restore` drill
- one schema-required startup smoke gate before DB-backed validation

For the final Story 76 launch sign-off that reuses this workflow, use:

- [Render public-beta launch checklist](./render-public-beta-launch-checklist.md)

## What Story 75 Includes

- one tracked helper script:
  - `python scripts/postgres_backup_restore.py backup`
  - `python scripts/postgres_backup_restore.py restore`
- one explicit manual migration-release checklist
- one explicit restore-drill checklist

## What Story 75 Does Not Include

- no automatic deploy hook for migrations
- no zero-downtime cutover tooling
- no point-in-time recovery orchestration
- no second admin console or release service

Those stay out of scope for the current beta bar.

## Migration Release Checklist

Use this workflow for any beta release that includes one or more Alembic revisions.

1. Capture a logical backup from the current source database before touching migrations:

```bash
python scripts/postgres_backup_restore.py backup --output <path-to-backup.dump>
```

By default this reads `DATABASE_URL`.

2. Deploy the new app revision.

3. Apply schema changes manually in the target environment:

```bash
alembic upgrade head
```

4. Require schema readiness before calling DB-backed routes healthy:

```bash
python scripts/render_startup_smoke.py --require-schema
```

Expected result:

```text
render_startup_smoke_ok schema_ready=true current_revision=<head> head_revision=<head>
```

5. Verify process health:

```text
GET /health
```

6. Verify one DB-backed route or page, such as sign-in start or a signed-in creator page.

## Restore Drill Checklist

Use this workflow on a scratch Postgres database, not on the live beta database.

1. Capture a logical backup from the source database:

```bash
python scripts/postgres_backup_restore.py backup --output <path-to-backup.dump>
```

2. Restore that backup into a blank or disposable scratch database:

```bash
python scripts/postgres_backup_restore.py restore --database-url <scratch-database-url> --input <path-to-backup.dump>
```

3. Point `DATABASE_URL` at the restored scratch database.

4. Re-run the schema-required startup smoke:

```bash
python scripts/render_startup_smoke.py --require-schema
```

Expected result:

- the restore command prints `postgres_restore_ok`
- the schema-required smoke passes on the restored database

That is the minimum believable beta recovery proof for Story 75.

## Notes

- `scripts/postgres_backup_restore.py` strips the password from the CLI connection URL and passes it through `PGPASSWORD` so secrets do not need to appear in the process arguments.
- The helper expects PostgreSQL URLs. It intentionally rejects non-Postgres URLs.
- The startup-smoke gate remains the same seam introduced in Stories 73 and 74; Story 75 reuses it rather than inventing a second readiness check.
