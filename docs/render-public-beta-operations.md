# Render Public-Beta Operations

This runbook defines the minimum operator workflow for the first public beta on Render.

It intentionally stays small:

- one Render web service
- one same-region managed Postgres database
- one single app instance
- existing app surfaces and scripts only
- lightweight external docs instead of a separate admin console or external observability stack

## Operator Surfaces

Use these surfaces in this order before reaching for ad hoc database inspection:

1. Render deploy status, service health, and service logs
2. `GET /health`
3. `python scripts/render_startup_smoke.py`
4. `python scripts/render_startup_smoke.py --require-schema`
5. signed-in creator pages:
   - `/app/health`
   - `/app/attention`
   - `/app/reports`
   - `/app/experiments`
6. authenticated `GET /reports/health` when raw JSON is more useful than the browser page
7. structured app logs keyed by `request_id` and the relevant creator, booking, invoice, or provider identifiers

## Standard Checks

### 1. Process Health

```text
GET /health
```

Expected result:

```json
{"status":"ok"}
```

Use this for process liveness only. It is not enough to prove that DB-backed routes are ready on a fresh database.

### 2. Startup Smoke

Basic config-plus-database check:

```bash
python scripts/render_startup_smoke.py
```

Expected result:

```text
render_startup_smoke_ok schema_ready=<true|false> current_revision=<revision|none> head_revision=<revision|none>
```

Schema-required check before DB-backed validation:

```bash
python scripts/render_startup_smoke.py --require-schema
```

Expected result:

- success only when the current database revision matches the repo migration head
- failure if the app can reach Postgres but migrations have not been applied yet

### 3. Creator-Scoped Health

Sign in as the creator through the normal browser flow, then open:

```text
/app/health
```

This page shows the current creator-scoped counts and reasons for:

- unattributed bookings
- Calendly ingress backlog or failure
- payment-provenance backlog
- blocked billing
- authoritative-content lag

Use:

- `/app/attention` for blocked billing and unmatched payment details
- `/app/reports` for the paid view and backlog explanation
- `/app/experiments` to confirm whether helper output is ready or unsupported

If raw JSON is needed and a Bearer token is already available:

```text
GET /reports/health
```

## Failure Playbooks

## Deploy Or Bootstrap Failure

Signals:

- Render marks the deploy unhealthy or failed
- `/health` does not return `200`
- `python scripts/render_startup_smoke.py` fails
- `python scripts/render_startup_smoke.py --require-schema` fails with schema not at head

Actions:

1. Check the latest Render deploy logs first.
2. Confirm the required non-local env vars are present and not placeholder values.
3. Run:

```bash
python scripts/render_startup_smoke.py
```

4. If that succeeds but `--require-schema` fails, run:

```bash
alembic upgrade head
python scripts/render_startup_smoke.py --require-schema
```

5. Re-check one DB-backed path such as sign-in start after the schema-required smoke passes.

## Magic-Link Auth Failure

Signals:

- sign-in page status banners such as invalid or expired link, or retry
- structured auth logs:
  - `magic_link_start_delivery_failed`
  - `magic_link_verify_failed`
  - `magic_link_start_rate_limited`

Actions:

1. Reproduce through the normal sign-in page first.
2. Capture the `request_id` from the response header.
3. Search logs by that `request_id` and the affected email.
4. If delivery failed, confirm the SMTP env vars in Render before retrying.
5. If verify failed, request a fresh link and repeat; expired or used links are intentionally rejected with the same safe response.

## Calendly Ingress Backlog Or Failure

Signals:

- `/app/health` shows non-zero Calendly backlog or failure counts
- structured webhook logs such as:
  - `calendly_webhook_event_processing_failed`
  - `calendly_webhook_booking_created_missing_tid`
  - `calendly_webhook_booking_created_unknown_tid`
  - `calendly_webhook_booking_canceled_missing_booking`

Actions:

1. Open `/app/health` to confirm whether the issue is backlog, failure, or both.
2. Search logs by `request_id`, `calendly_event_id`, or `calendly_booking_uuid`.
3. If the event was deferred or failed and the journal row id is known, reprocess it safely:

```bash
python scripts/reprocess_calendly_event.py --journal-id <journal_row_id>
```

4. Re-check `/app/health` after reprocessing.
5. If the issue is missing or unknown `tid`, repair the tracked-link path or content linkage before expecting the counts to clear.

## Stripe Payment Backlog Or Noop Signals

Signals:

- `/app/health` shows payment backlog
- `/app/attention` shows unmatched payment rows
- structured Stripe logs such as:
  - `stripe_webhook_invoice_paid_recorded_unmatched`
  - `stripe_webhook_invoice_paid_duplicate_event`
  - `stripe_webhook_invoice_paid_noop_already_paid`
  - `stripe_webhook_invoice_paid_noop_non_open`

Actions:

1. Open `/app/attention` and `/app/reports` to confirm whether the payment is unmatched or already safely ignored.
2. Search logs by `stripe_event_id`, `stripe_invoice_id`, and `stripe_account_id`.
3. Treat duplicate and noop signals as idempotent safety outcomes, not immediate incidents.
4. Treat unmatched signals as backlog until the local invoice or booking linkage is repaired.

## Blocked Billing

Signals:

- `/app/health` shows blocked billing counts
- `/app/attention` shows an open blocked billing card
- structured logs include:
  - `blocked_billing_case_created`
  - `blocked_billing_case_updated`
  - `blocked_billing_case_resolved`

Actions:

1. Open `/app/attention`.
2. Confirm the reason code and provider context on the blocked case card.
3. Repair the actual underlying issue first, such as Stripe readiness or provider availability.
4. Use the built-in retry only after the condition changes:

```text
POST /app/attention/blocked-billing/{case_id}/retry
```

5. Confirm the case moves to recovered or still-blocked without creating duplicate invoices.

## Helper Unsupported Or Degraded State

Signals:

- `/app/experiments` remains `unsupported`
- `/app/health` shows authoritative-content lag, payment backlog, blocked billing, or unattributed-booking counts

Actions:

1. Start with `/app/experiments` to confirm whether the helper is unsupported or simply lacks current grounded evidence.
2. Use `/app/health` to identify which upstream seam is still degraded.
3. Use `/app/content` for authoritative-evidence gaps and `/app/attention` for blocked billing or unmatched payment cases.
4. Do not treat unsupported helper output as a separate AI failure before the underlying evidence or ingress signals are clear.

## Notes

- For beta, Render-native deploy or uptime signals are the only required automated alerting baseline.
- The runbook assumes the existing structured logs remain the primary event-trace source.
- This workflow is intentionally narrower than a full dashboard or SRE program.
