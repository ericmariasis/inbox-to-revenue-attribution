# Growth Loop Agent Submission Script

## Demo Thesis

The Growth Loop Agent connects engagement intelligence to commercial truth. It does not just recommend another campaign. It checks whether the previous path produced paid students, then prepares one next action for human review.

## One-Minute Talk Track

1. Start on the seeded creator workspace.
   - "This tutor workspace already has one tracked content row, one attributed booking, and one PayPal-shaped paid result."

2. Open `Growth Loop`.
   - "The agent reads two kinds of context. Loomi Marketing and Analytics MCP-shaped diagnostics provide engagement intelligence, while this app owns the attribution and paid-result evidence."

3. Point to `Agent console`.
   - "The first screen is built for judges: Signal to Proof to Action. Bloomreach/Loomi supplies the recoverable audience signal, the app verifies paid truth, and the agent prepares one reviewed next action with a measurement boundary."

4. Click through `Run agent`.
   - "This is the controlled agent run. It inspects paid proof, reads the Loomi schema evidence, scores candidate actions, prepares the recovery brief, generates the Bloomreach-ready segment recipe, and attaches the measurement plan."

5. Point to `Review packet`.
   - "This is the compact artifact a judge or marketer can review: selected recovery action, Bloomreach-ready segment recipe, measurement plan, proof chain, and the no-send/no-mutation/no-lift boundaries."

6. Open `Evidence appendix`.
   - "The first screen stays short, but the full proof, recipes, and boundaries are still here when a reviewer wants to inspect them."

7. Point to the cross-system demo map.
   - "For this local demo, the page stays deterministic, but we also have live Loomi proof from Cursor MCP. Cursor authenticated to Loomi, discovered the sleepy-goose project, and inspected the live event schema."

8. Point to `Live Loomi schema proof`.
   - "The sandbox does not have saved segmentations or recommendations yet, so the strongest live signal is its event schema. The agent turns that schema into a cart-abandon recovery blueprint, then maps the same pattern back to this app's booking-step recovery model."

9. Point to `Reviewable recovery brief`.
   - "The next artifact is the human-review brief: target segment, message outline, draft Bloomreach segment spec, success evidence, diagnostic signals, and copy-ready recovery text. It is useful enough to hand to a marketer, but still does not send or mutate anything."

10. Point to `Decision trace`.
   - "The trace shows the agentic reasoning without pretending to execute anything. It scores the recovery brief above a broad nurture follow-up and blocks direct Bloomreach mutation because this slice is review-only."

11. Point to `Bloomreach-ready segment recipe`.
   - "The next artifact is the exact recipe a marketer could manually recreate in Bloomreach: who to include, who to exclude, the 24-hour recovery window, message variables, and how to measure later paid results. It still does not create a saved segment, send a campaign, or mutate Bloomreach."

12. Point to `Measurement plan`.
   - "The agent also defines how the loop would be measured after review: paid revenue from app-owned invoice and payment records, a withheld holdout first, a 24-hour recovery send window, and a 7-day paid-outcome observation window. It explicitly says no lift is claimed yet."

13. Point to the diagnosis and prepared action.
   - "The agent classifies the workspace as `Paid Result Exists`, identifies the next reviewed action, and prepares a follow-up brief instead of mutating an external system."

14. Point to the evidence boundary.
   - "Tracked content, bookings, canonical invoices, and payment-backed records stay separate from Loomi context. That keeps the claim honest."

15. Open Reports, then `Why this revenue counted`.
   - "The paid outcome can be inspected through the reporting evidence chain: tracked content to booking to paid invoice to supporting PayPal-shaped capture event."

## Track 6 Framing

- Loomi Connect MCP-shaped diagnostics: read-side engagement and analytics context.
- Cursor-authenticated Loomi MCP proof: live `sleepy-goose` project discovery and event-schema inspection.
- App-owned attribution layer: tracked content, booking links, bookings, invoices, and payment events.
- Payment-backed outcome layer: PayPal-shaped paid result evidence for the demo seed.
- Agentic action layer: a bounded, copy-ready recovery brief for human review.
- Judge-presentation layer: a compact first-screen `Signal -> Proof -> Action` path that keeps the demo legible in 90 seconds.
- Guided-run layer: visible understand -> decide -> prepare behavior inside the product demo.
- Review-packet layer: one review-ready artifact that packages the selected action, segment recipe, proof chain, and boundaries.
- Evidence-appendix layer: deeper proof, recipes, and limits available on demand without overwhelming the first screen.
- Decision-trace layer: deterministic candidate ranking that explains why recovery was selected and direct mutation was blocked.
- Segment-recipe layer: a review-only Bloomreach recreation recipe with include/exclude/window/measure logic.
- Measurement-plan layer: a no-lift-yet plan that would evaluate later paid revenue through app-owned invoice/payment records against a withheld holdout.

## Claims To Make

- "The demo shows cross-system orchestration from engagement context to paid-result evidence to reviewed action planning."
- "The live Loomi proof showed a rich commerce event schema, and the app turns that into a reviewable cart-abandon recovery blueprint."
- "The agent turns that blueprint into a concrete recovery brief with target segment, message outline, draft Bloomreach segment spec, and evidence plan."
- "The console makes the hackathon capability obvious in one screen: Signal -> Proof -> Action, followed by a bounded measurement plan."
- "The guided run makes the agent behavior visible: it steps from paid proof to Loomi schema evidence to action scoring to a review packet."
- "The review packet gives judges one compact artifact that preserves the no-send, no-mutation, no-lift-yet boundaries."
- "The evidence appendix lets reviewers inspect the full proof stack without forcing every judge through a long dossier."
- "The decision trace ranks candidate actions with schema fit, app evidence fit, and review safety, so judges can see why recovery was selected."
- "The segment recipe translates the selected recovery action into include, exclude, 24-hour window, message-variable, and measurement guidance that a marketer could recreate manually in Bloomreach."
- "The measurement plan keeps the loop honest: paid revenue is the primary metric, holdout comparison is preferred, and campaign or retargeting engagement is diagnostic only."
- "The app keeps revenue truth grounded in canonical booking, invoice, and payment records."
- "Loomi context informs the recommendation but does not replace reporting truth."
- "The action is prepared for review; it is not sent automatically."

## Claims To Avoid

- Do not say Loomi proved revenue causality.
- Do not say `/app/growth-loop` makes a live Loomi page-load call.
- Do not say the fixture diagnostics are production customer data.
- Do not say the schema-derived cart-abandon blueprint is a saved Loomi segmentation or campaign.
- Do not say the copy-ready recovery brief was sent, exported, or created inside Bloomreach.
- Do not say the copy button sends, exports, syncs, or creates anything.
- Do not say the segment recipe was saved, exported, or created inside Bloomreach.
- Do not say the measurement plan reports measured lift, causal impact, statistical confidence, or revenue improvement.
- Do not say the agent sends campaigns or mutates external systems.
- Do not say the decision scores are measured lift, statistical confidence, or live LLM output.
- Do not say the Conversation MCP proof supplied support, checkout, booking, refund, or payment-failure telemetry; it only exposed catalog-proxy signals and was deferred.
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
