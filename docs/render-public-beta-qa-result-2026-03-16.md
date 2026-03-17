# Render Public-Beta QA Result - 2026-03-16

This records one full Render beta QA pass using the structure from [Render public-beta master QA plan](./render-public-beta-master-qa-plan.md).

Environment:

- Base URL: `https://inbox-to-revenue-attribution-web.onrender.com/`
- Date run: `2026-03-16` America/New_York
- Launch outcome: `Launch-ready`

## Beta QA Result

- Date: `2026-03-16`
- Build / commit: Render deployed build at `https://inbox-to-revenue-attribution-web.onrender.com/`
- Environment(s): Render beta / production deployment
- Tester(s): manual operator-led pass
- Launch gate pass: yes
- Operator lane pass: yes
- Creator UX lane pass: yes, with a few subchecks not exercised
- Abuse/support lane pass: yes for the Render-safe path
- Workspace(s) used:
  - one cold-start workspace
  - one ready-to-track, no-paid workspace
  - one warm-path paid workspace
  - one dedicated QA workspace for a real support request
- Source URL(s) used:
  - `https://careercodepro.substack.com/p/one-small-change-that-instantly-improves`
- Support request id(s):
  - `af56d680-558a-4d91-8a93-b2ca3c07ff30`
- Backlog owner and next review time:
  - not required for this pass because backlog counts were zero

## Launch Baseline

Checks:

- `GET /health` returned `{"status":"ok"}`
- `python scripts/render_startup_smoke.py --require-schema` returned:

```text
render_startup_smoke_ok schema_ready=true current_revision=3c4d5e6f7a8b head_revision=3c4d5e6f7a8b
```

Result:

- deploy and schema baseline passed

## Operator Lane

Browser and JSON health were coherent:

- `/app/health` rendered successfully
- `/app/attention` rendered successfully
- authenticated `GET /reports/health` returned:
  - `booking_attribution.unattributed_booking_count = 0`
  - `calendly_ingress.backlog_event_count = 0`
  - `calendly_ingress.failed_event_count = 0`
  - `payment_provenance.current_backlog_event_count = 0`
  - `blocked_billing.open_case_count = 0`
  - `authoritative_content.lagging_content_count = 0`

Log traceability passed:

- representative request id: `061b2828-139a-442e-9d72-751c678ea7f8`
- related creator id visible in Render logs: `e41ae739-1596-4fd8-addc-468110bb6eb3`

Result:

- operator warm path passed

## Creator UX Lane

Cold-start setup:

- booking link creation succeeded
- Stripe connect succeeded
- tracked content creation succeeded
- `/app` reached:
  - `Ready to track`
  - `Waiting for first paid result`
- the setup flow required no operator or DB help

Account and danger-zone trust:

- `/app/account` sections were clear
- self-serve versus support-assisted boundaries were clear
- real support-request submit proof passed
- support inbox received:
  - request type: `workspace-reset`
  - request id: `af56d680-558a-4d91-8a93-b2ca3c07ff30`

Real content and tracked-link understanding:

- the real public Substack URL was accepted successfully
- the saved row showed the source URL and generated tracked link
- tracked-link usage was understandable in the UI

Waiting-state and first-value proof:

- the ready-to-track, no-paid workspace showed `Waiting for first paid result`
- the reports page showed `Illustrative preview`
- the preview was clearly illustrative only and not live workspace revenue

Warm-path reporting and trust:

- the warm-path workspace showed paid results clearly
- paid truth remained visibly separate from blocked and unmatched diagnostic states

Experiments honesty:

- the unsupported path was exercised and was honest
- the page clearly said the helper was unsupported because not enough trusted evidence existed yet
- the `ready` path and evidence drilldown were not exercised in this pass

UX finding discovered during the run:

- the normal content page exposed a `Review topics for this content` link that led creators into a page requiring internal `fetch and extract` prerequisites with no clear self-serve action
- a local fix now removes that normal creator-facing entry point from the content card; deploy that fix before broader beta traffic

## Abuse / Support Regression Lane

Render-safe path exercised:

- one real support request was submitted successfully from `/app/account`
- the operator queue loaded successfully at `/app/operator/support-requests`
- the request appeared with the correct id, type, requester context, and email delivery state
- the operator detail page loaded successfully
- the request status was changed to `In Review`
- the creator-facing account page reflected the updated `In Review` state

Result:

- abuse/support regression lane passed for the safe deployed-app subset

## Not Exercised In This Pass

- same-device sign-in comprehension copy check
- experiments `ready` path plus evidence drilldown
- preview or local-only abuse checks such as forced email-delivery failure, duplicate-submit pressure, or repeated auth/redirect limiter pressure

## Recommendation

Status:

- ready for a controlled beta with real users after deploying the already-prepared topic-review hide/remove fix

Why:

- the deployed app passed the main launch, operator, creator-setup, reporting, and support-workflow checks
- no launch blocker surfaced in this Render run
- the only concrete UX issue discovered in the deployed app already has a narrow low-risk fix implemented locally

Suggested release posture:

- start with a small trusted beta cohort
- deploy the topic-review hide/remove fix first
- keep the remaining unexercised checks as follow-up QA rather than blockers unless a new issue appears
