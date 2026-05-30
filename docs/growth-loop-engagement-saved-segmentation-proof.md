# Growth Loop Engagement Saved Segmentation Proof

## Goal

Create one harmless Bloomreach Engagement saved segmentation as proof that the Growth Loop recovery recipe can become a real Bloomreach object, then display only sanitized object metadata in the local app.

## Target Object

- Primary object type: saved segmentation
- Target name: `CCP Cart Recovery Demo - 2026-05-29`
- Project: `sleepy-goose`
- Workspace: `Hackathon Workspace`
- Creation surface: Bloomreach Engagement UI

## Recorded Result

- Saved object created: `Yes`
- Object type: Bloomreach Engagement saved segmentation
- Exact object name: `CCP Cart Recovery Demo - 2026-05-29`
- Object ID / stable identifier: `6a19e7fdf98a9214fd6a5960`
- Project: `sleepy-goose`
- Workspace: `Hackathon Workspace`
- Creation surface: Bloomreach Engagement UI
- Created by: Eric Mariasis
- Last changed: `2026-05-29 15:24`
- Saved UI definition captured: `cart_update` event with `total_quantity > 0`
- Unsupported or intentionally omitted clauses: later completed-purchase exclusion and 24-hour recovery window were not encoded in this first saved object; those remain review-packet recipe guidance.
- Activation boundary: no campaign, flow, send, export, checkout, payment, Storefront mutation, or app page-load Bloomreach mutation was performed.

## App Display Validation

- Result: `Pass`
- Date: `2026-05-29`
- Seeded workspace: `growth-loop-demo-4cacd9a3@example.com`
- `/app/growth-loop` displayed the recorded saved-segmentation proof in the judge cockpit and in the `Bloomreach saved segment proof` detail.
- The detail showed `CCP Cart Recovery Demo - 2026-05-29` / `6a19e7fdf98a9214fd6a5960`, `sleepy-goose` / `Hackathon Workspace`, and `Created in Engagement UI`.
- Runtime copy stated the app displays sanitized metadata only and does not create, update, delete, send, export, checkout, payment, or mutate Bloomreach/Storefront on page load.
- The review packet continued to keep app-owned invoice and payment records as the paid-result truth.

## Safety Boundaries

- Do not send or schedule any campaign.
- Do not create a flow, recommendation, checkout, payment, export, Storefront change, or AWS/GCP resource.
- Do not commit private URLs, customer records, raw payloads, screenshots with customer data, cookies, tokens, or credentials.
- Do not claim lift, causality, revenue improvement, or that the saved segment is paid truth.

## Segment Intent

Use the Story 132 recipe if the UI supports the required fields:

- Include customers or visitors with `cart_update` activity where `total_quantity > 0`.
- Prefer higher-intent context when available: checkout starts, product IDs, cart value, or `view_item` category activity.
- Exclude anyone with a later completed `purchase` inside a 24-hour recovery window.
- Keep app-owned paid invoices and payment-backed records as the only paid-result truth in the demo.

If the UI cannot express all clauses, save the closest harmless segmentation only when its limitations can be stated clearly.

## Proof Fields To Capture

Record only these sanitized fields:

- Saved object created: `Yes` or `No`
- Object type
- Exact object name
- Object ID or stable object identifier, if visible
- Project/workspace
- Creation surface
- Unsupported or approximated clauses

## App Configuration

Use the proof values only if a real object exists:

```powershell
$env:GROWTH_LOOP_BLOOMREACH_SEGMENT_PROOF_ENABLED='true'
$env:GROWTH_LOOP_BLOOMREACH_SEGMENT_PROOF_NAME='CCP Cart Recovery Demo - 2026-05-29'
$env:GROWTH_LOOP_BLOOMREACH_SEGMENT_PROOF_ID='6a19e7fdf98a9214fd6a5960'
$env:GROWTH_LOOP_BLOOMREACH_SEGMENT_PROOF_PROJECT_NAME='sleepy-goose'
$env:GROWTH_LOOP_BLOOMREACH_SEGMENT_PROOF_WORKSPACE_NAME='Hackathon Workspace'
$env:GROWTH_LOOP_BLOOMREACH_SEGMENT_PROOF_CREATED_VIA='Bloomreach Engagement UI'
$env:GROWTH_LOOP_BLOOMREACH_SEGMENT_PROOF_STATUS_LABEL='Created in Engagement UI'
```

Do not enable the proof card with a placeholder ID.

## Fallback

If the Engagement UI cannot create a saved segmentation, try one harmless dashboard or report artifact next and record that result separately. Do not force a dashboard/report into the saved-segment proof env vars.
