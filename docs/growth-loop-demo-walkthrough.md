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
- Saved-segment proof can be captured through a sanctioned Bloomreach UI/API path. Configure only sanitized segment name/ID metadata; the app page must not create or mutate Bloomreach on load.

## Saved Segment Proof

If Bloomreach Engagement UI or another sanctioned Bloomreach path creates a real saved segment, set these additional variables before seeding and running the app:

```powershell
$env:GROWTH_LOOP_BLOOMREACH_SEGMENT_PROOF_ENABLED='true'
$env:GROWTH_LOOP_BLOOMREACH_SEGMENT_PROOF_NAME='<saved segment name from Bloomreach>'
$env:GROWTH_LOOP_BLOOMREACH_SEGMENT_PROOF_ID='<saved segment id or stable object identifier from Bloomreach>'
$env:GROWTH_LOOP_BLOOMREACH_SEGMENT_PROOF_PROJECT_NAME='sleepy-goose'
$env:GROWTH_LOOP_BLOOMREACH_SEGMENT_PROOF_WORKSPACE_NAME='Hackathon Workspace'
$env:GROWTH_LOOP_BLOOMREACH_SEGMENT_PROOF_CREATED_VIA='Bloomreach Engagement UI'
$env:GROWTH_LOOP_BLOOMREACH_SEGMENT_PROOF_STATUS_LABEL='Created in Engagement UI'
```

Do not enable these values without a real saved segment ID. If no object was created, leave them unset and use the default review-only path.

Current proof note: on `2026-05-29`, Cursor MCP discovery found no write/create tool for saved segments, segmentations, customer filters, or autosegments. Story 140 then created one harmless saved segmentation through Bloomreach Engagement UI: `CCP Cart Recovery Demo - 2026-05-29` / `6a19e7fdf98a9214fd6a5960`. The saved object encodes `cart_update` where `total_quantity > 0`; later completed-purchase exclusion and the 24-hour window remain review-packet recipe guidance. Proof-enabled app browser validation passed with the recorded metadata; the app displays sanitized proof only and performs no page-load Bloomreach mutation.

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
- The first judge-facing artifact is `Agent console`.
- The console opens with `Judge demo cockpit` and the `Signal -> Proof -> Action` story.
- The `90-second judge path` shows:
  - `Bloomreach/Loomi signal`
  - `Sandbox proof`
  - `Real Bloomreach object proof` when saved-segment proof metadata is configured
  - the app-owned paid proof
  - a `Review-ready action`
  - the `Measurement boundary`
- The `Sandbox proof` card connects `Pacific Apparel` Storefront context, `sleepy-goose` Engagement surfaces, and app-owned paid truth without claiming a live page-load call.
- When configured, the `Real Bloomreach object proof` card shows the saved segment name, object ID, `sleepy-goose` / `Hackathon Workspace`, `Bloomreach Engagement UI`, and a direct `Inspect saved segment proof` link.
- The console includes `Run agent` as the primary guided workflow.
- The guided workflow steps are:
  - `Inspect paid proof`
  - `Read Loomi schema evidence`
  - `Score candidate actions`
  - `Prepare recovery brief`
  - `Generate segment recipe`
  - `Attach measurement plan`
- Clicking `Run next step` advances the workflow and then changes to `View review packet`.
- The run completion says `Review packet assembled` and keeps the no-send/no-export/no-mutation boundary.
- The console includes `View review packet` plus anchor shortcuts for `Proof`, `Sandbox`, `Bloomreach object` when configured, `Action`, `Segment`, `Measure`, `Boundaries`, and `Evidence appendix`.
- The `Review packet` is visible near the top of the page and summarizes the selected action, segment recipe, measurement plan, proof chain, and boundaries.
- The review packet states that no campaign is sent, no lift is claimed yet, and app-owned invoice/payment records remain paid truth.
- If saved-segment proof metadata is configured, the review packet states that no Bloomreach object is created or changed by the page load and that the recorded saved segment remains review-only.
- If saved-segment proof metadata is not configured, the review packet stays in the default no-Bloomreach-mutation mode.
- The `Evidence appendix` is collapsed by default.
- Opening the appendix shows `Full proof stack for reviewers`.
- The appendix includes a `Sandbox proof` detail panel with `Pacific Apparel shopping context`, `sleepy-goose activation and measurement`, and `App-owned paid truth`.
- The sandbox proof boundary states that the page makes no live Engagement or Storefront call, embeds no customer data/screenshots/raw payloads/private URLs, performs no external mutation, and claims no lift, causality, or new paid-truth source.
- When configured, the `Bloomreach saved segment proof` detail shows the saved segment name/ID, states where it was created, and states this page only displays recorded metadata.
- The detail panels inside the appendix preserve the deeper proof artifacts without making the first screen a long packet.
- The diagnosis is `Paid proof exists; choose the next reviewed action.`
- The next action is `Prepare one follow-up brief from the proven path`.
- The stage is `Paid Result Exists`.
- The page includes a `Live Loomi schema proof` detail panel with `Verified via Cursor MCP`.
- The schema opportunity is `Cart-abandon recover & convert`.
- The opportunity is bridged back to this app as a `Booking-step recovery analogue`.
- The page includes a `Reviewable recovery brief` action detail.
- The recovery brief includes:
  - `Target segment`
  - `Message outline`
  - `Draft Bloomreach segment spec`
  - `Success evidence`
  - `Diagnostic signals`
  - `Copy-ready recovery brief`
- The recovery brief includes `Copy review brief`, and its boundary says the copy affordance does not send, export, or mutate Bloomreach.
- The page includes `Rule-backed decision trace` and `Decision trace`.
- The decision trace ranks:
  - `Booking-step recovery brief` as `Selected` with `9/10`
  - `Broad nurture follow-up` as `Held for later` with `6/10`
  - default path: `Direct Bloomreach segment or campaign mutation` as `Blocked in this slice` with `3/10`
  - saved-segment proof path: `Direct in-app Bloomreach mutation` remains kept out of runtime while the recorded segment proves the mutation path
- The decision trace includes `Schema fit`, `App evidence fit`, `Review safety`, and `Evidence chain`.
- The decision trace states that no live LLM call is required, no campaign is sent, no Bloomreach object is mutated, and Loomi diagnostics do not become paid truth.
- The page includes `Bloomreach-ready segment recipe`.
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
- The page includes `Measurement plan`.
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
2. The Agent console is the judge-friendly cockpit: `Signal -> Proof -> Action` is visible before the deeper evidence stack.
3. Click through `Run agent`: the workflow visibly inspects paid proof, reads Loomi schema evidence, scores actions, prepares the brief, generates the segment recipe, and attaches measurement.
4. The app owns the commercial truth: tracked content, attributed booking, local invoice, and PayPal-shaped capture event.
5. Cursor MCP provides the live Loomi proof: the authenticated `sleepy-goose` project has a rich commerce event schema.
6. The review packet turns that proof into a selected recovery action, a Bloomreach-ready segment recipe, and a no-lift-yet measurement plan.
7. The Sandbox proof shows where the live sandbox fits: Pacific Apparel supplied shopping/cart/offer context, Engagement supplied event and activation surfaces, and the app still owns paid-result truth.
8. Open the `Evidence appendix` only when a judge wants the underlying proof, recipes, and boundaries.
9. The in-app schema blueprint is deterministic and review-only; it does not claim the page made a live Loomi, Engagement, or Storefront call.
10. The agent converts the blueprint into a copy-ready recovery brief that a human can review before recreating the segment or campaign in Bloomreach.
11. The decision trace shows why the recovery brief beats a broad nurture follow-up and why direct Bloomreach mutation is blocked in this slice.
12. The segment recipe shows exactly what a marketer could manually recreate in Bloomreach: include logic, exclude logic, a 24-hour recovery window, message variables, and measurement guardrails.
13. If saved-segment proof metadata is configured, point to `Real Bloomreach object proof` in the first judge path, then open `Bloomreach saved segment proof`: the saved segment proves the mutation path, while this page still performs no page-load mutation.
14. The measurement plan shows how a marketer would evaluate the reviewed recovery loop: paid revenue first, holdout comparison, 24-hour send window, 7-day paid-outcome observation, and diagnostic-only engagement signals.
15. The agent does not claim causal lift or invent paid truth.
16. The agent prepares one reviewed next action from the proven path instead of sending or mutating anything autonomously.

## Optional Report Cross-Check

Open `REPORTS_URL` and confirm that the paid result is counted from app-owned invoice/payment evidence. Provider identity should be PayPal-shaped through the provider-neutral invoice and payment-event fields.

## Cleanup

The seed writes to the configured local database. Use the normal local test database reset/truncate workflow before running unrelated manual checks.
