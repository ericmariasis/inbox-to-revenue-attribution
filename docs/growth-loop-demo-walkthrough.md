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
- Directly below the schema proof, the page includes `Reviewable recovery brief`.
- The recovery brief includes:
  - `Target segment`
  - `Message outline`
  - `Draft Bloomreach segment spec`
  - `Success evidence`
  - `Diagnostic signals`
  - `Copy-ready recovery brief`
- Directly below the recovery brief, the page includes `Rule-backed decision trace` and `Decision trace`.
- The decision trace ranks:
  - `Booking-step recovery brief` as `Selected` with `9/10`
  - `Broad nurture follow-up` as `Held for later` with `6/10`
  - `Direct Bloomreach segment or campaign mutation` as `Blocked in this slice` with `3/10`
- The decision trace includes `Schema fit`, `App evidence fit`, `Review safety`, and `Evidence chain`.
- The decision trace states that no live LLM call is required, no campaign is sent, no Bloomreach object is mutated, and Loomi diagnostics do not become paid truth.
- Directly below the decision trace, the page includes `Bloomreach-ready segment recipe`.
- The segment recipe includes:
  - `Include`
  - `Exclude`
  - `24-hour recovery window`
  - `Message variables`
  - `Measure`
  - `Conversation MCP note`
  - `Review boundary`
- The segment recipe states that it is a review-only manual recreation recipe, does not create a saved Bloomreach segment/campaign/recommendation, does not send a campaign, and does not mutate any external system.
- The segment recipe measures later success through app-owned paid invoice/payment-backed records and treats campaign/retargeting events as diagnostic context only.
- The Conversation MCP note says the proof was deferred because it exposed catalog-proxy signals rather than support, checkout, booking, refund, or payment-failure telemetry.
- Directly below the segment recipe, the page includes `Measurement plan`.
- The measurement plan includes:
  - paid revenue as the primary metric
  - paid conversion rate / paid invoice count as supporting context
  - a withheld holdout as the preferred comparison
  - a 24-hour recovery send window
  - a 7-day paid-outcome observation window
  - campaign and retargeting engagement as diagnostic context only
  - a visible `No lift yet` boundary
- The measurement plan states that the app does not report measured lift, causal impact, or revenue improvement until a campaign runs and app-owned paid outcomes are compared.
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
- The recovery brief boundary states that it is prepared for human review only, does not mutate Bloomreach, does not create saved segments/campaigns/recommendations, and does not count revenue or prove causality.

## Demo Talk Track

1. This is a real signed-in creator workspace in the existing app, not a standalone prototype.
2. The app owns the commercial truth: tracked content, attributed booking, local invoice, and PayPal-shaped capture event.
3. Cursor MCP provides the live Loomi proof: the authenticated `sleepy-goose` project has a rich commerce event schema.
4. The in-app schema blueprint is deterministic and review-only; it does not claim the page made a live Loomi call.
5. The agent converts the blueprint into a copy-ready recovery brief that a human can review before recreating the segment or campaign in Bloomreach.
6. The decision trace shows why the recovery brief beats a broad nurture follow-up and why direct Bloomreach mutation is blocked in this slice.
7. The segment recipe shows exactly what a marketer could manually recreate in Bloomreach: include logic, exclude logic, a 24-hour recovery window, message variables, and measurement guardrails.
8. The measurement plan shows how a marketer would evaluate the reviewed recovery loop: paid revenue first, holdout comparison, 24-hour send window, 7-day paid-outcome observation, and diagnostic-only engagement signals.
9. The agent does not claim causal lift or invent paid truth.
10. The agent prepares one reviewed next action from the proven path instead of sending or mutating anything autonomously.

## Optional Report Cross-Check

Open `REPORTS_URL` and confirm that the paid result is counted from app-owned invoice/payment evidence. Provider identity should be PayPal-shaped through the provider-neutral invoice and payment-event fields.

## Cleanup

The seed writes to the configured local database. Use the normal local test database reset/truncate workflow before running unrelated manual checks.
