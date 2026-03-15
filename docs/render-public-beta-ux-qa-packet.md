# Render Public-Beta UX QA Packet

This packet complements the Render launch checklist.

Use it when you need confidence that the beta is understandable and usable through the actual creator UI, not only that the deploy, schema, and operator seams are healthy.

The launch checklist remains the release gate. This packet answers a different question:

- can a real creator understand what the beta does
- can they complete the core setup without operator translation
- can they use a realistic piece of content such as a public blog or Substack post
- can they understand what the reporting, attention, and experiments surfaces are telling them

## Relationship To The Launch Checklist

Use this packet alongside:

- [Render public-beta launch checklist](./render-public-beta-launch-checklist.md)

Use the launch checklist for:

- deploy and schema readiness
- operator checks
- final launch-gate status

Use this UX packet for:

- creator comprehension
- cold-start onboarding confidence
- realistic manual browser QA
- value communication and trust checks

## What This Packet Covers

- first-impression clarity and product-promise comprehension
- cold-start creator setup through the browser
- realistic content setup using a real public article URL
- tracked-link usage understanding
- warm-path reporting and trust surfaces
- experiments helper honesty in both `ready` and `unsupported` states
- key empty states and failure states a beta creator is likely to hit

## What This Packet Does Not Replace

- the launch checklist
- Story 75 migration and recovery checks
- Story 60 provider-backed Stripe `invoice.paid` proof
- broader operator runbooks
- performance or load testing

## Test Setup

Prefer using two creator workspaces if possible:

1. a cold-start workspace with little or no existing data
2. a warm-path workspace with at least:
   - one booking link
   - connected Stripe
   - one tracked content row
   - one paid result or one honest empty state

Have at least one realistic public content URL ready.

Recommended examples:

- a normal public blog post
- a public Substack post
- another public article page you control

That URL should be:

- reachable without login
- reachable without a paywall
- served as public HTML or text
- the real page where a creator would place a booking CTA

Optional negative-case URL:

- a paywalled post
- a login-only page
- a page that returns an unsupported content type

## Evidence To Capture

For each lane below, capture:

- the creator workspace used
- screenshots or notes for the key page states
- the exact public source URL used
- the generated tracked link
- the tester's own explanation of what the product does
- any confusion about terms such as `booking link`, `tracked link`, `reports`, `attention`, or `unsupported`
- whether the issue is cosmetic, confusing, or launch-blocking

## UX QA Lanes

### 1. First Impression And Product Promise

Goal:

- confirm a creator can understand the beta's value without operator interpretation

Steps:

1. Open the sign-in flow and complete browser sign-in.
2. Land on `/app`.
3. Ask the tester to explain, in their own words:
   - what this product does
   - what setup is required before it becomes useful
   - what result would prove it is working

Expected result:

- the tester can explain a version of:
  - "This tells me which posts led to bookings and paid revenue."
- the tester understands that setup depends on:
  - a booking link
  - Stripe connection
  - a tracked content link
- the tester does not mistake the product for a generic analytics dashboard or a generic AI-writing tool

### 2. Cold-Start Creator Setup

Goal:

- confirm the browser flow is usable for a brand-new creator

Steps:

1. Start from a cold-start workspace.
2. Complete sign-in.
3. Open `/app` and review the setup progress state.
4. Create a booking link in `/app/booking-links`.
5. Start Stripe connection from the browser flow.
6. Open `/app/content` and create the first tracked content row.
7. Return to `/app` after each step and confirm the setup progress still makes sense.

Expected result:

- setup progress is legible
- the next step is obvious after each completed action
- blocked or incomplete setup states explain what is still missing
- the creator can reach a "core setup is ready" state without needing DB or operator help

### 3. Real Content Source Fit

Goal:

- confirm the beta works with realistic creator content, not only synthetic example URLs

Steps:

1. Open `/app/content`.
2. Paste a real public article URL from a blog or Substack post.
3. Choose the matching booking link.
4. Generate the tracked link.
5. Confirm the saved content row renders the source URL and the generated tracked link.
6. If the content workflow for fetch, extraction, or topic review is available for that workspace, continue into the topic-review page and confirm the source still makes sense there.

Expected result:

- a real public blog or Substack URL is accepted as content input
- the creator can see the source URL they entered
- the creator can copy the generated tracked link
- the system behaves honestly if the content cannot be fetched or reviewed

Important interpretation:

- the source URL is the article or post itself
- the tracked link is the redirect URL the creator should place in the booking CTA, button, or call-to-action they share from that content
- the beta does not retroactively attribute past traffic that did not go through the tracked link

### 4. Tracked-Link Usage Understanding

Goal:

- confirm the creator understands where the tracked link belongs

Steps:

1. On the content page, show the tester both:
   - the public source URL
   - the generated tracked link
2. Ask the tester:
   - which URL is the article URL
   - which URL should go into the CTA or booking button
   - what they would paste into their blog post or Substack CTA

Expected result:

- the tester understands that the article URL stays the canonical content URL
- the tester understands that the tracked link belongs in the outbound booking CTA path
- the tester does not confuse the tracked link with the page URL they are publishing

### 5. Warm-Path Reporting And Trust

Goal:

- confirm the reporting surfaces answer the core beta question clearly

Steps:

1. Use a warm-path workspace with real or seeded activity.
2. Open `/app/reports`.
3. Open `/app/attention` if unmatched or blocked items exist.
4. Open `/app/bookings` if booking activity context helps explain the state.
5. Ask the tester:
   - which content produced paid results
   - why a blocked or unmatched item is not counted yet
   - what they should do next if something is waiting on recovery

Expected result:

- the tester can identify which content generated paid results
- paid truth is visibly separate from blocked, unmatched, or not-yet-counted states
- the next action is understandable when attention items exist
- the page feels evidence-backed rather than vague

### 6. Experiments Helper Honesty

Goal:

- confirm the helper feels trustworthy in both supported and unsupported states

Steps:

1. Open `/app/experiments` on a workspace that should be `unsupported`.
2. Confirm the unsupported explanation is specific and honest.
3. If a ready workspace exists, generate a ready run.
4. Open one evidence page:
   - `/app/experiments/{run_claim_snapshot_id}/cards/{card_order}`
5. Ask the tester:
   - why the helper is unsupported, if unsupported
   - what evidence the ready suggestion is based on, if ready

Expected result:

- `unsupported` does not look like a crash or empty placeholder
- `ready` does not look like generic advice
- the evidence page clearly ties the suggestion back to authoritative content and settled paid results

### 7. Empty States And Failure States

Goal:

- confirm the most likely beta gaps still feel understandable

Check these states where possible:

- no booking links yet
- Stripe not connected or not billable yet
- no content yet
- no paid results yet
- blocked billing visible in attention
- unmatched payment visible in attention or reports
- unsupported content URL or fetch failure

Expected result:

- the UI explains what is missing
- the next practical action is visible
- the creator does not need operator-only vocabulary to understand the state

## Pass Criteria

Treat the UX packet as a pass when all of these are true:

- a tester can explain the product benefit in plain language
- a cold-start creator can complete the core browser setup without operator translation
- a realistic public blog or Substack URL works as a tracked content source, or any failure is clearly explained
- the tester understands where to place the tracked link
- reporting makes paid truth and not-yet-counted states understandable
- the experiments helper is honest in both `ready` and `unsupported` states

## Escalate As A Beta Blocker

Treat a UX finding as a real beta blocker only if it crosses one of these lines:

- the creator cannot understand what the product does after normal browser setup
- the creator cannot tell how to use the tracked link
- a normal public blog or Substack post consistently fails despite meeting the stated public-URL assumptions
- reporting materially misleads the creator about what counted as paid truth
- unsupported helper output looks like a broken or misleading AI feature rather than an intentional not-ready state

## Suggested Outcome Record

Capture the result in this shape:

```text
Beta UX QA result:
- Date:
- Build:
- Tester workspace(s):
- Real source URL(s) used:
- Product promise understood: yes/no
- Cold-start setup pass: yes/no
- Real content source fit pass: yes/no
- Tracked-link usage understood: yes/no
- Reporting/trust pass: yes/no
- Experiments honesty pass: yes/no
- Blockers found:
- Follow-ups:
```
