# Growth Loop Bloomreach Customer Property Proof

## Goal

Record one sanitized proof point that a real Bloomreach Engagement customer-property activation path was exercised outside the app, then display that proof inside the Growth Loop review packet without making the app mutate Bloomreach on page load.

## Recorded Proof Shape

- Project: `sleepy-goose`
- Workspace: `Hackathon Workspace`
- Creation surface: `Bloomreach Engagement UI`
- Customer property: `ccp_growth_loop_recovery_candidate`
- Recorded value: `story142_review_ready`
- Customer label: sanitized operator-provided label only; do not commit raw customer data, email, cookie, profile URL, or event payload.

This proof is intentionally separate from revenue evidence. It shows that the activation surface can carry the selected recovery audience marker, while app-owned bookings, invoices, and payment-backed records remain the paid-result truth.

## Local Configuration

Enable only after a real customer-property update has been performed in the Bloomreach sandbox:

```powershell
$env:GROWTH_LOOP_BLOOMREACH_ACTIVATION_PROOF_ENABLED='true'
$env:GROWTH_LOOP_BLOOMREACH_ACTIVATION_PROOF_CUSTOMER_LABEL='<sanitized customer label>'
$env:GROWTH_LOOP_BLOOMREACH_ACTIVATION_PROOF_PROPERTY_NAME='ccp_growth_loop_recovery_candidate'
$env:GROWTH_LOOP_BLOOMREACH_ACTIVATION_PROOF_PROPERTY_VALUE='story142_review_ready'
$env:GROWTH_LOOP_BLOOMREACH_ACTIVATION_PROOF_PROJECT_NAME='sleepy-goose'
$env:GROWTH_LOOP_BLOOMREACH_ACTIVATION_PROOF_WORKSPACE_NAME='Hackathon Workspace'
$env:GROWTH_LOOP_BLOOMREACH_ACTIVATION_PROOF_CREATED_VIA='Bloomreach Engagement UI'
$env:GROWTH_LOOP_BLOOMREACH_ACTIVATION_PROOF_STATUS_LABEL='Recorded activation proof'
```

Leave these values disabled or unset if no real customer-property update has been completed.

## App Display

When configured, `/app/growth-loop` shows:

- a first-path `Live activation proof` card in the Agent console
- an `Activation proof` anchor shortcut
- a `Bloomreach customer property proof` appendix section
- review-packet proof-chain language that includes the customer property and keeps the paid-result boundary intact

## Boundaries

- The app displays recorded metadata only.
- The page does not create, update, or delete Bloomreach customer properties on load.
- No campaign, flow, send, export, checkout, payment, or Storefront mutation is triggered by this proof.
- The recorded customer-property proof does not count revenue or prove lift/causality.
- App-owned invoice and payment records remain paid truth.
