# Growth Loop Agent Submission Script

## Demo Thesis

The Growth Loop Agent connects engagement intelligence to commercial truth. It does not just recommend another campaign. It checks whether the previous path produced paid students, then prepares one next action for human review.

## One-Minute Talk Track

1. Start on the seeded creator workspace.
   - "This tutor workspace already has one tracked content row, one attributed booking, and one PayPal-shaped paid result."

2. Open `Growth Loop`.
   - "The agent reads two kinds of context. Loomi Marketing and Analytics MCP-shaped diagnostics provide engagement intelligence, while this app owns the attribution and paid-result evidence."

3. Point to the cross-system demo map.
   - "For this local demo, the page stays deterministic, but we also have live Loomi proof from Cursor MCP. Cursor authenticated to Loomi, discovered the sleepy-goose project, and inspected the live event schema."

4. Point to `Live Loomi schema proof`.
   - "The sandbox does not have saved segmentations or recommendations yet, so the strongest live signal is its event schema. The agent turns that schema into a cart-abandon recovery blueprint, then maps the same pattern back to this app's booking-step recovery model."

5. Point to `Reviewable recovery brief`.
   - "The next artifact is the human-review brief: target segment, message outline, draft Bloomreach segment spec, success evidence, diagnostic signals, and copy-ready recovery text. It is useful enough to hand to a marketer, but still does not send or mutate anything."

6. Point to the diagnosis and prepared action.
   - "The agent classifies the workspace as `Paid Result Exists`, identifies the next reviewed action, and prepares a follow-up brief instead of mutating an external system."

7. Point to the evidence boundary.
   - "Tracked content, bookings, canonical invoices, and payment-backed records stay separate from Loomi context. That keeps the claim honest."

8. Open Reports, then `Why this revenue counted`.
   - "The paid outcome can be inspected through the reporting evidence chain: tracked content to booking to paid invoice to supporting PayPal-shaped capture event."

## Track 6 Framing

- Loomi Connect MCP-shaped diagnostics: read-side engagement and analytics context.
- Cursor-authenticated Loomi MCP proof: live `sleepy-goose` project discovery and event-schema inspection.
- App-owned attribution layer: tracked content, booking links, bookings, invoices, and payment events.
- Payment-backed outcome layer: PayPal-shaped paid result evidence for the demo seed.
- Agentic action layer: a bounded, copy-ready recovery brief for human review.

## Claims To Make

- "The demo shows cross-system orchestration from engagement context to paid-result evidence to reviewed action planning."
- "The live Loomi proof showed a rich commerce event schema, and the app turns that into a reviewable cart-abandon recovery blueprint."
- "The agent turns that blueprint into a concrete recovery brief with target segment, message outline, draft Bloomreach segment spec, and evidence plan."
- "The app keeps revenue truth grounded in canonical booking, invoice, and payment records."
- "Loomi context informs the recommendation but does not replace reporting truth."
- "The action is prepared for review; it is not sent automatically."

## Claims To Avoid

- Do not say Loomi proved revenue causality.
- Do not say `/app/growth-loop` makes a live Loomi page-load call.
- Do not say the fixture diagnostics are production customer data.
- Do not say the schema-derived cart-abandon blueprint is a saved Loomi segmentation or campaign.
- Do not say the copy-ready recovery brief was sent, exported, or created inside Bloomreach.
- Do not say the agent sends campaigns or mutates external systems.
- Do not say PayPal-shaped local evidence is a live PayPal transaction.

## Manual Proof Path

Use `docs/growth-loop-demo-walkthrough.md` for setup commands and expected browser checkpoints.

The strongest proof sequence is:

1. `/app`
2. `/app/growth-loop`
3. `/app/reports`
4. `/app/reports/explanations/paid/{tracking_id}`

Optional live proof sequence before the app walkthrough:

1. Open Cursor MCP settings and confirm `loomi-mcp` is authenticated.
2. Ask Cursor to list Bloomreach orgs/workspaces/projects.
3. Ask Cursor to inspect `sleepy-goose` and summarize the event schema.
4. Return to `/app/growth-loop` and show how the verified schema proof becomes a reviewable opportunity blueprint.
