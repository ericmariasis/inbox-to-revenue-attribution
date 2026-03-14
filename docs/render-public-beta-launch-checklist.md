# Render Public-Beta Launch Checklist

This checklist is the Story 76 closeout path for the first public beta on Render.

It intentionally reuses the existing Story 73-75 surfaces instead of inventing a second launch workflow:

- Render deploy status and logs
- `GET /health`
- `python scripts/render_startup_smoke.py --require-schema`
- authenticated `GET /reports/health`
- signed-in creator pages in the existing app shell
- the tracked Story 73-75 runbooks

Story 60 live provider-backed Stripe `invoice.paid` proof remains a separate hard launch gate. Story 76 does not absorb it, but this checklist must report whether it is already satisfied or is still the single remaining blocker.

For the underlying beta environment and operator workflows, use:

- [Render public-beta bootstrap](./render-public-beta-bootstrap.md)
- [Render public-beta operations](./render-public-beta-operations.md)
- [Render public-beta migration and recovery](./render-public-beta-migration-recovery.md)

## What Story 76 Includes

- one explicit Render launch checklist
- one warm-path creator-visible sign-off flow
- one operator-visible sign-off flow
- one explicit final launch outcome:
  - launch-ready, or
  - Render beta slice passed and Story 60 remains the single remaining blocker

## What Story 76 Does Not Include

- no new admin console
- no new telemetry stack
- no second migration workflow
- no requirement to rerun a full cold-start creator flow if an existing warm-path beta workspace is already available
- no absorption of the Story 60 live provider-backed Stripe proof into this story

## Preconditions

1. The chosen beta build is deployed to Render.
2. If the deployed revision includes Alembic migrations, the Story 75 migration workflow is available:
   - logical backup
   - `alembic upgrade head`
   - `python scripts/render_startup_smoke.py --require-schema`
3. One signed-in creator workspace exists on Render with:
   - connected Stripe
   - at least one tracked content row
   - at least one reporting row or an honest empty state
4. Render service logs and shell access are available.
5. A creator login and, if needed, a Bearer token for `GET /reports/health` are available.

## Launch Checklist

### 1. Deploy And Schema Baseline

1. Confirm the Render deploy is healthy.
2. If the release includes schema changes, follow the Story 75 migration release checklist first.
3. Run:

```bash
python scripts/render_startup_smoke.py --require-schema
```

Expected result:

```text
render_startup_smoke_ok schema_ready=true current_revision=<head> head_revision=<head>
```

4. Verify process liveness:

```text
GET /health
```

Expected result:

```json
{"status":"ok"}
```

### 2. Creator Warm Path

Sign in through the deployed app, then confirm:

1. `/app`
   - setup state renders successfully
   - attention links are present when blocked or unmatched work exists
2. `/app/content`
   - tracked content is visible
3. `/app/reports`
   - paid results or honest empty state renders
   - unmatched or blocked explanations remain separate from paid truth
4. `/app/experiments`
   - helper state is honest:
     - `ready`, with evidence drilldown available, or
     - `unsupported`, with the established unsupported explanation
5. `/app/health`
   - creator-scoped health snapshot renders
6. `/app/attention`
   - blocked billing or unmatched payment details render when relevant

If the chosen workspace has a ready helper run, also open one evidence page:

```text
/app/experiments/{run_claim_snapshot_id}/cards/{card_order}
```

### 3. Operator Warm Path

1. Confirm the runbooks match the deployed environment:
   - [Render public-beta bootstrap](./render-public-beta-bootstrap.md)
   - [Render public-beta operations](./render-public-beta-operations.md)
   - [Render public-beta migration and recovery](./render-public-beta-migration-recovery.md)
2. Call the creator-scoped JSON health surface:

```text
GET /reports/health
```

3. Confirm the returned health snapshot is coherent with the browser surfaces.
4. Confirm Render or app logs are sufficient to trace at least one representative request or issue using `request_id` and the relevant creator, booking, invoice, or provider identifiers.

### 4. Story 60 Launch Gate Status

Record one of these outcomes explicitly:

#### Outcome A — Launch-ready

Use this only if the Story 60 live provider-backed Stripe `invoice.paid` proof has already been rerun successfully on the same beta environment or launch account context.

#### Outcome B — Single Remaining Blocker

Use this exact outcome if Story 60 is still unresolved:

```text
Render beta slice passed. Story 60 live provider-backed Stripe invoice.paid proof remains the single remaining blocker before public launch.
```

When Outcome B applies, include this rerun handoff:

1. Align the Stripe platform API key, Connect client id, dashboard session, and Stripe CLI login to the same platform account.
2. Rerun the Story 60 live provider-backed `invoice.paid` proof against the beta environment or same launch account context.
3. Update the launch record with the pass or fail result before declaring public launch ready.

## Launch Decision

Declare the beta launch gate satisfied only when:

- the Render deploy and schema baseline are healthy
- the required creator warm-path surfaces render successfully
- the required operator warm-path surfaces render successfully
- Story 60 live provider-backed Stripe `invoice.paid` proof is complete

If the Render checklist passes but Story 60 remains unresolved, treat Story 76 as the completed Render sign-off story and Story 60 as the only remaining launch blocker.
