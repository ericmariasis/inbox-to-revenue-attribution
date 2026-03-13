# Render Public-Beta Bootstrap

This repo's first public-beta deployment path is a single Render web service plus one same-region managed Postgres instance.

## What Story 73 Includes

- one Render blueprint in `render.yaml`
- one pinned Python runtime in `.python-version`
- one startup smoke command in `scripts/render_startup_smoke.py`
- one schema-aware startup smoke result that can optionally fail when migrations are not at head
- one explicit non-local config checklist for the Render service

## What Story 73 Does Not Include

- no worker process
- no Redis or other shared state
- no autoscaling
- no dashboard or alerting policy
- no backup or restore automation
- no automatic migration workflow choice

Those remain for later Phase 14 stories.

For the current Story 74 operator workflow, use:

- [Render public-beta operations](./render-public-beta-operations.md)

For the current Story 75 migration and recovery workflow, use:

- [Render public-beta migration and recovery](./render-public-beta-migration-recovery.md)

## Target Shape

- web service: `inbox-to-revenue-attribution-web`
- database: `inbox-to-revenue-attribution-db`
- region: `virginia`
- web plan: `starter`
- database plan: `basic-256mb`
- database access: private-network only (`ipAllowList: []`)

## Required Render Environment Values

The blueprint sets safe fixed values where possible and prompts for the rest.

Prompted values (`sync: false`):

- `STRIPE_CONNECT_CLIENT_ID`
- `STRIPE_SECRET_KEY`
- `STRIPE_CONNECT_REDIRECT_URI`
- `STRIPE_WEBHOOK_SECRET`
- `CALENDLY_WEBHOOK_SIGNING_KEY`
- `TRACKED_LINK_BASE_URL`
- `MAGIC_LINK_BASE_URL`
- `MAGIC_LINK_EMAIL_FROM_EMAIL`
- `MAGIC_LINK_EMAIL_SMTP_HOST`
- `MAGIC_LINK_EMAIL_SMTP_USERNAME`
- `MAGIC_LINK_EMAIL_SMTP_PASSWORD`

Generated value:

- `JWT_SECRET`

Fixed values from the blueprint:

- `APP_ENV=production`
- `JWT_ALGORITHM=HS256`
- `STRIPE_CONNECT_AUTHORIZE_URL=https://connect.stripe.com/oauth/authorize`
- `MAGIC_LINK_EMAIL_PROVIDER=smtp`
- `MAGIC_LINK_EMAIL_FROM_NAME=Creator Compass`
- `MAGIC_LINK_EMAIL_SMTP_PORT=587`
- `MAGIC_LINK_EMAIL_SMTP_STARTTLS=true`
- `MAGIC_LINK_EMAIL_SMTP_USE_SSL=false`
- `DATABASE_URL` from the managed Postgres connection string

Use the same public base host for:

- `TRACKED_LINK_BASE_URL`
- `MAGIC_LINK_BASE_URL`
- `STRIPE_CONNECT_REDIRECT_URI` (with `/stripe/connect/callback`)

## Bootstrap Steps

1. Create the Render Blueprint from this repo's `render.yaml`.
2. Provide all prompted secret and host values during creation.
3. Wait for the initial deploy to finish.
4. Run the startup smoke command from a Render shell or one-off job:

```bash
python scripts/render_startup_smoke.py
```

Expected result:

- output begins with `render_startup_smoke_ok`
- `schema_ready=false` is expected on a brand-new database before migrations run

5. If you need to prove DB-backed routes are ready, require the schema to be at head:

```bash
python scripts/render_startup_smoke.py --require-schema
```

This fails safely until the current database revision matches the repo migration head.

6. For a brand new database, run migrations manually before broader app validation:

```bash
alembic upgrade head
```

This story documents that command but does not automate it. Migration workflow hardening stays in Story 75.

7. Verify the service health endpoint:

```text
GET /health
```

Expected result:

```json
{"status":"ok"}
```

## Notes

- The app already blocks unsafe non-local startup in `app.core.config`.
- The startup smoke command is still the narrow bootstrap check, but it now also reports whether the current database schema is at the repo migration head.
- Single-instance deployment is intentional because redirect soft limiting is still process-local in the current codebase.
- For creator-scoped triage after deploy, use the Story 74 browser page at `/app/health`.
