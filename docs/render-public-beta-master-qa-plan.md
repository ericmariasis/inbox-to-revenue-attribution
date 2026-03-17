# Render Public-Beta Master QA Plan

This is the top-level QA structure for the current public beta.

It composes the existing launch, operator, migration, recovery, and creator UX docs instead of replacing them.

## Decision

- use one master beta QA plan above the existing packet and runbooks
- keep the launch checklist as the release gate
- keep the UX packet as the detailed creator-browser lane
- keep operator and migration guidance in their existing runbooks
- do not split into multiple new QA stories by default

Only split later if reruns uncover a concrete missing artifact that this plan still cannot cover cleanly, such as:

- no repeatable workspace or data setup for one required lane
- no safe preview or local path for abuse or support-failure regression
- no evidence or result-capture format that lets later reruns compare outcomes reliably

## Current QA Assets

Use these docs together:

- [Render public-beta bootstrap](./render-public-beta-bootstrap.md)
- [Render public-beta launch checklist](./render-public-beta-launch-checklist.md)
- [Render public-beta operations](./render-public-beta-operations.md)
- [Render public-beta migration and recovery](./render-public-beta-migration-recovery.md)
- [Render public-beta UX QA packet](./render-public-beta-ux-qa-packet.md)

Useful automated anchors already in the repo:

- `tests/test_phase14_validation.py::test_phase14_launch_surfaces_cover_warm_creator_and_operator_paths`
- `tests/test_phase10_5_validation.py::test_phase10_5_self_serve_trust_flow_end_to_end`
- `tests/test_ui_auth_shell.py`

## What Phase 14.5 And Phase 15 Added

The current beta QA docs were already strong on deploy, migration, operator recovery, and core creator UX. The main remaining packaging gap is that newer trust and abuse/support seams are spread across stories rather than one top-level QA structure.

Phase 14.5 added:

- verify-first magic-link identity creation
- shared Postgres-backed auth and redirect rate limiting
- durable support-request persistence and duplicate handling
- one minimal allowlisted operator queue for support-assisted account requests

Phase 15 added:

- same-device browser sign-in guidance
- one canonical readiness vocabulary across setup surfaces
- plain-language blocked and unmatched diagnostic framing
- a read-only illustrative first-value proof for ready-to-track creators with no paid results yet

The existing UX packet already covers most creator-facing QA, but it did not by itself answer:

- where the abuse and support regression lane belongs
- which checks should run on the launch environment versus preview or local
- how one combined beta QA result should be captured

## Entry Criteria

Before running the full beta QA pass, confirm:

1. the candidate build or commit is known
2. the target environment is deployed and reachable
3. operator access exists for logs, `/health`, and authenticated `GET /reports/health`
4. if the build contains schema changes, the migration workflow is ready
5. at least the workspace matrix below can be assembled or approximated safely

## Workspace Matrix

Prefer these test states:

1. Cold-start workspace
   - little or no existing data
2. Ready-to-track, no-paid workspace
   - Stripe connected
   - at least one booking link with amount and currency
   - at least one tracked content row
   - zero paid invoices
3. Warm-path workspace
   - at least one paid result and, if possible, one blocked or unmatched backlog item
4. Dedicated support-request workspace
   - safe to submit one real reset or deletion request if the check runs on Render
5. Allowlisted operator session
   - separate browser profile or private window for `/app/operator/support-requests`

These can overlap when necessary, but keep the support-request workspace disposable enough that one real manual-review request is acceptable.

## Environment Split

Do not force every QA lane onto the live beta environment.

Use the launch environment for:

- deploy and schema baseline
- operator warm-path checks
- creator UX checks
- one happy-path support-request submit if a real request is acceptable

Use preview or local for:

- repeated auth-abuse or redirect-rate-limit checks
- forced email-delivery failure states
- duplicate submit and invalid transition regressions
- any rerun that depends on DB inspection or repeated limiter pressure

## QA Lanes

### 1. Launch Gate And Deploy Baseline

Primary docs:

- [Render public-beta launch checklist](./render-public-beta-launch-checklist.md)
- [Render public-beta bootstrap](./render-public-beta-bootstrap.md)

Use this lane to prove:

- deploy health
- schema readiness
- creator warm-path baseline
- operator warm-path baseline
- final Story 60 launch-gate status

Required evidence:

- `python scripts/render_startup_smoke.py --require-schema` result
- `GET /health` result
- launch outcome: ready, or single remaining blocker

### 2. Operator QA And Recovery Lane

Primary docs:

- [Render public-beta operations](./render-public-beta-operations.md)
- [Render public-beta migration and recovery](./render-public-beta-migration-recovery.md)

Use this lane to prove:

- creator-scoped health and JSON health stay coherent
- unmatched and blocked backlog ownership is explicit when counts are non-zero
- the current operator can trace one representative issue with `request_id` and provider ids
- migration and restore guidance is still usable when the release shape requires it

Required evidence:

- `/app/health` and `GET /reports/health` alignment
- designated backlog owner and next review time when backlog is non-zero
- any migration or restore drill notes that apply to the tested release

### 3. Creator UX And Trust Lane

Primary doc:

- [Render public-beta UX QA packet](./render-public-beta-ux-qa-packet.md)

Use this lane to prove:

- same-device sign-in guidance is understandable
- cold-start setup works with one canonical readiness vocabulary
- account and danger-zone boundaries feel honest
- real content-source handling still fits realistic creator input
- the reports waiting state and illustrative first-value proof stay honest
- warm-path reports, attention, and experiments remain evidence-backed

Required evidence:

- tester explanation of the product promise
- source URL and tracked link used
- workspace(s) used for cold-start, ready-to-track, and warm-path checks
- screenshots or notes for any confusing or misleading state

### 4. Abuse And Support Regression Lane

This is the new lane the older docs did not package clearly. Keep it targeted and pragmatic.

Run these checks:

1. Same-device auth sanity
   - confirm sign-in start and invalid-link states explain the same-device or same-browser expectation
2. Verify-first auth sanity
   - in preview or local, confirm a new-email sign-in start does not leave a durable creator before verify succeeds
3. Shared limiter sanity
   - in preview or local, confirm repeated sign-in or redirect traffic still follows the safe user-facing contract while limiter hits remain operator-visible
4. Support-request durability
   - submit one reset or deletion request
   - capture the request id
   - confirm the same request remains visible on `/app/account`
5. Operator queue lifecycle
   - open `/app/operator/support-requests` as an allowlisted operator
   - confirm the request is visible with delivery state
   - move one request into `in_review` or a terminal state
   - confirm the creator-facing account page reflects the updated saved status

Required evidence:

- one request id
- one operator queue screenshot or note
- one creator account screenshot or note after operator review
- any limiter or auth log evidence captured from preview or local reruns

## Execution Order

Use this order unless a release-specific reason requires something narrower:

1. rerun the automation anchors relevant to the build
2. run the launch checklist on the target environment
3. run the operator lane
4. run the creator UX packet
5. run the abuse and support regression lane in the safest environment for those checks
6. capture one combined outcome record

## Combined Outcome Record

Use one record for the whole pass:

```text
Beta QA result:
- Date:
- Build / commit:
- Environment(s):
- Tester(s):
- Launch gate pass: yes/no
- Operator lane pass: yes/no
- Creator UX lane pass: yes/no
- Abuse/support lane pass: yes/no
- Workspace(s) used:
- Source URL(s) used:
- Support request id(s):
- Backlog owner and next review time:
- Automated checks rerun:
- Blockers:
- Follow-ups:
```

## When To Create More QA Stories

Do not create multiple QA stories just to mirror these lanes.

Create another story only if a rerun shows one concrete gap that deserves its own implementation slice, for example:

- a reusable workspace or seed-data setup artifact is missing
- the abuse/support lane needs a dedicated preview harness or a safer repeatable fixture
- the combined result record is still too weak for future reruns or launch decisions

Right now the recommended next step is one packaging pass with this master plan, not story sprawl.
