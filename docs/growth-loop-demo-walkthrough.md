# Growth Loop Agent Demo Walkthrough

## Goal

Seed one local paid PayPal-shaped demo workspace and use it to show the Growth Loop Agent moving from app-owned paid proof plus Loomi diagnostic context to one reviewed next action.

## Preconditions

- Run from the repo root.
- Local test database is available through `TEST_DATABASE_URL`.
- Use a local or test-safe app environment only.
- Story 124 Growth Loop Agent code is present.
- The browser walkthrough remains deterministic; no live MCP token is required.
- Optional live proof can be captured in Cursor MCP before the walkthrough. The app section should describe that proof as verified schema evidence, not as a live page-load MCP call.

## Seed The Demo Workspace

```powershell
$env:APP_ENV='manual_test'
$env:MAGIC_LINK_EMAIL_PROVIDER='stub'
$env:MAGIC_LINK_BASE_URL='http://127.0.0.1:8000'
$env:GROWTH_LOOP_AGENT_FEATURE_ENABLED='true'
if (-not $env:TEST_DATABASE_URL) { $env:TEST_DATABASE_URL='postgresql+psycopg://postgres:math1991@localhost:5434/attribution_test' }
$env:DATABASE_URL=$env:TEST_DATABASE_URL
.venv\Scripts\python.exe scripts\seed_growth_loop_demo.py --base-url http://127.0.0.1:8000
```

The command prints:

- `LOGIN_URL`
- `BACKUP_LOGIN_URL`
- `GROWTH_LOOP_URL`
- `APP_URL`
- `REPORTS_URL`
- `EXPECTED_STAGE=Paid Result Exists`
- `EXPECTED_REVENUE=$195.00`

## Run The Local App

```powershell
$env:APP_ENV='manual_test'
$env:MAGIC_LINK_EMAIL_PROVIDER='stub'
$env:MAGIC_LINK_BASE_URL='http://127.0.0.1:8000'
$env:GROWTH_LOOP_AGENT_FEATURE_ENABLED='true'
if (-not $env:TEST_DATABASE_URL) { $env:TEST_DATABASE_URL='postgresql+psycopg://postgres:math1991@localhost:5434/attribution_test' }
$env:DATABASE_URL=$env:TEST_DATABASE_URL
.venv\Scripts\uvicorn.exe app.main:app --host 127.0.0.1 --port 8000
```

Open `LOGIN_URL` in the browser, then open `GROWTH_LOOP_URL`.

## Expected Growth Loop Screen

- The page title is `Growth Loop Agent`.
- The active nav item is `Growth Loop`.
- The diagnosis is `Paid proof exists; choose the next reviewed action.`
- The next action is `Prepare one follow-up brief from the proven path`.
- The stage is `Paid Result Exists`.
- The page includes `Live Loomi schema proof` with `Verified via Cursor MCP`.
- The schema opportunity is `Cart-abandon recover & convert`.
- The opportunity is bridged back to this app as a `Booking-step recovery analogue`.
- The event/property proof includes:
  - `cart_update.total_quantity`
  - `purchase.purchase_status`
  - `campaign.status`
  - `retargeting.action`
- App-owned evidence shows:
  - `1 content item`
  - `1 booking`
  - `1 paid invoice`
  - `$195.00`
- Loomi context is labeled `Loomi fixture diagnostics`.
- Limits state that revenue remains canonical invoice/payment truth from this app, Loomi diagnostics are not a second paid-result ledger, and no external mutation is executed.
- The schema opportunity boundary states that it is not a live page-load MCP call, does not send campaigns, and does not count revenue or prove causality.

## Demo Talk Track

1. This is a real signed-in creator workspace in the existing app, not a standalone prototype.
2. The app owns the commercial truth: tracked content, attributed booking, local invoice, and PayPal-shaped capture event.
3. Cursor MCP provides the live Loomi proof: the authenticated `sleepy-goose` project has a rich commerce event schema.
4. The in-app schema blueprint is deterministic and review-only; it does not claim the page made a live Loomi call.
5. The agent does not claim causal lift or invent paid truth.
6. The agent prepares one reviewed next action from the proven path instead of sending or mutating anything autonomously.

## Optional Report Cross-Check

Open `REPORTS_URL` and confirm that the paid result is counted from app-owned invoice/payment evidence. Provider identity should be PayPal-shaped through the provider-neutral invoice and payment-event fields.

## Cleanup

The seed writes to the configured local database. Use the normal local test database reset/truncate workflow before running unrelated manual checks.
