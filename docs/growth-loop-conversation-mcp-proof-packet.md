# Growth Loop Conversation MCP Proof Packet

## Purpose

Use Cursor as the official MCP client to prove whether the Bloomreach Conversation Tools MCP endpoint can add useful recovery-intent signals to the Growth Loop demo.

This is a proof/discovery packet only. It does not add app runtime code, store credentials, query the endpoint from FastAPI, send campaigns, mutate Bloomreach, or change app-owned paid truth.

## Decision Gate

Proceed to an app-facing Conversation diagnostic layer only if the proof finds at least three useful intent signals that clearly refine the current booking-step recovery action and are stronger than generic catalog/search proxies.

A useful signal must have:

- a short signal label
- the tool/resource or result family that produced it
- why it matters for recovery
- how it changes or sharpens the reviewable recovery brief
- a boundary note confirming it is diagnostic-only

If this threshold is not met, the next implementation slice should pivot to the Bloomreach-ready segment recipe instead.

If the endpoint works but only returns catalog-level proxy signals, record the outcome as `weak` and pivot to the Bloomreach-ready segment recipe.

## Known Context

- Bloomreach project: `sleepy-goose`
- Project ID: `b15c09b0-5469-11f1-b333-862b79b06b65`
- Existing Growth Loop action: `Booking-step recovery brief`
- Existing selected action score: `9/10`
- Existing app-owned proof: one tracked content item, one booking, one paid invoice, and `$195.00`
- Existing boundary: Loomi and Conversation signals remain diagnostic context only; app-owned booking, invoice, and payment records remain paid truth.

## Safety Rules

- Do not commit raw private MCP endpoint URLs, access tokens, browser callback URLs, cookies, or auth artifacts.
- Do not paste raw customer transcripts or personally identifying customer details into this file.
- Prefer aggregate or paraphrased findings.
- Do not claim Conversation MCP proves revenue, causality, attribution, or lift.
- Do not claim `/app/growth-loop` made a live Conversation MCP call.

## Cursor Setup Checklist

Use Cursor MCP settings, not app code.

1. Add a custom MCP server for the Conversation Tools endpoint from the private Slack/operator message.
2. Authenticate through the browser flow if Cursor prompts for auth.
3. Confirm Cursor shows the server as connected or lists enabled tools/resources.
4. Run the prompts below in Cursor chat.
5. Record only sanitized summaries in the proof result table.

## Cursor Prompts

### 1. Server And Tool Inventory

```text
Using the Conversation Tools MCP server, list the available tools and resources. Keep the response concise. Include tool/resource names when available, but do not include credentials, callback URLs, cookies, or raw auth details.
```

### 2. Project Or Data Scope

```text
Using the Conversation Tools MCP server, identify whether you can access data related to the sleepy-goose project or the Hackathon Workspace. Return any project, workspace, organization, index, dataset, or collection names/IDs that are safe to display. If no project mapping is visible, say so clearly.
```

### 3. Recovery Intent Search

```text
Using the Conversation Tools MCP server, search for conversation or support-intent signals that could refine a cart-abandon / booking-step recovery brief. Focus on pricing, payment, checkout friction, cart abandonment, booking hesitation, scheduling, rescheduling, refunds, discounts, purchase blockers, and support tickets. Return aggregate or paraphrased findings only. Do not include raw customer transcripts or PII.
```

### 4. Three-Signal Gate

```text
Based only on tool-derived Conversation MCP results, decide whether there are at least three useful recovery-intent signals. For each useful signal, provide: signal label, source/tool/result family, why it matters, how it would refine the current booking-step recovery brief, and the diagnostic-only boundary. If fewer than three useful signals exist, say "threshold not met" and recommend pivoting to the Bloomreach-ready segment recipe.
```

### 5. App Integration Recommendation

```text
Given the Conversation MCP findings and the existing Growth Loop page, recommend either:
1. add a paid-result-only Conversation intent diagnostic layer next, or
2. pivot to the Bloomreach-ready segment recipe.

Keep the recommendation concise and explicitly state that Conversation signals do not count revenue, prove causality, mutate Bloomreach, or replace app-owned booking/invoice/payment evidence.
```

## Proof Result Template

Fill this section after running the Cursor proof.

### Run Metadata

- Date: 2026-05-28
- Operator: Eric Mariasis
- Cursor MCP server label: `conversation-mcp`
- Endpoint stored in tracked docs: no
- Auth status: connected in Cursor
- Tool/resource count: 4 tools, 0 resources

### Tool And Resource Summary

| Name | Type | Relevant To Recovery? | Notes |
| --- | --- | --- | --- |
| `seeker_products` | Tool | Partial proxy | Product/catalog context only; not support or conversation telemetry |
| `search_productCollections` | Tool | Partial proxy | Exposes collection/catalog search, not workspace mapping |
| `get_product` | Tool | Partial proxy | Product lookup can support catalog-specific copy, but does not show abandonment cause |
| `search_products` | Tool | Partial proxy | Returned catalog facets useful for price, discount, and availability proxy signals |
| None exposed | Resource | No | No resource descriptors were found for this server |

### Project / Scope Summary

- Organization: not visible
- Workspace: not visible
- Project or dataset: no `sleepy-goose` or Hackathon Workspace mapping visible
- Mapping confidence: none
- Notes: Safe catalog metadata was visible, including product collections such as `Harmony`, `Radiance`, `Mirage`, `Serendipity`, and `Seredipity`. Tooling appeared to be retail product catalog search rather than project/workspace, support, conversation, booking, checkout, refund, or payment-failure data.

### Useful Recovery-Intent Signals

| Signal | Source | Why It Matters | Recovery Brief Refinement | Boundary |
| --- | --- | --- | --- | --- |
| Price Sensitivity Bands | `search_products` refinement schema: price quartiles and refinement suggestions | Indicates likely budget vs premium hesitation at the catalog level | Branch recovery copy by price tier: value reassurance for lower tiers, quality or benefit reinforcement for higher tiers | Diagnostic proxy only; does not prove observed payment failure or true abandonment cause |
| Discount/Promo Responsiveness | `search_products` refinement schema: discount, sale, and special-offer facets | Promo metadata suggests offer-led nudges may reduce hesitation | Add an offer-aware variant such as a limited-time promo reminder or eligible-sale framing | Diagnostic proxy only; does not infer coupon errors, failed promo redemption, or individual discount expectations |
| Availability Friction Proxy | `search_products` refinement schema: availability and returned product sets | Availability certainty can reduce decision delay and abandonment risk | Add reassurance such as still-available language when app or channel data confirms it is true | Diagnostic proxy only; not live checkout inventory-lock evidence |

### Decision

Choose one after the proof:

- `pass`: at least three useful signals found; plan Conversation diagnostic layer next
- `weak`: endpoint works but signals are proxy-only or not strong enough for a Conversation diagnostic layer; pivot to segment recipe
- `blocked`: auth/tool/data access blocked; pivot to segment recipe and record blocker

Decision: `weak`

### Sanitized Notes

- Cursor authenticated to `conversation-mcp` successfully.
- The server exposed 4 tools and no resources.
- No raw endpoint URL, token, cookie, callback URL, auth artifact, raw transcript, or customer PII is stored in this packet.
- No `sleepy-goose` or Hackathon Workspace project mapping was visible from this server.
- The server appeared to expose a retail product-catalog/search context rather than a project/workspace registry or support/conversation corpus.
- No support tickets, chat transcripts, booking flows, checkout logs, refund cases, rescheduling events, or payment-failure telemetry were visible.
- Three useful proxy signals were available from catalog/search facets: price sensitivity bands, discount/promo responsiveness, and availability friction proxy.
- Cursor's final recommendation was to pivot to the Bloomreach-ready segment recipe and keep catalog-proxy signals as optional enrichment later.

## What This Proof Should Enable

If successful, the next app story can add a paid-result-only Conversation intent diagnostic section that explains how conversation/support intent sharpens the recovery message while preserving the existing no-send, no-mutation, no-causality, diagnostic-only boundary.

If weak or blocked, the next app story should add the Bloomreach-ready segment recipe using the already-proven Loomi schema path.
