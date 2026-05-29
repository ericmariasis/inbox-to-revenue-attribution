# Growth Loop Engagement + Storefront Sandbox Proof Packet

## Purpose

Use the operator-provided Bloomreach Engagement and Storefront sandbox access to prove whether these surfaces add useful, safe cross-system evidence to the Growth Loop demo.

This is a proof/discovery packet only. It does not add app runtime code, store credentials, send campaigns, create segments, publish storefront changes, run checkout, mutate Bloomreach or Storefront, or change app-owned paid truth.

## Decision Gate

Proceed to an app-facing Sandbox Proof Artifact only if the proof finds at least three safe cross-system facts that materially strengthen the Growth Loop story.

Each useful fact must have:

- a short fact label
- the source surface: Engagement, Storefront, or both
- a sanitized observed name, ID, or visible fact
- why it matters for the recovery loop
- how it strengthens the current Growth Loop demo
- a boundary note confirming it is diagnostic or demo context only

If the threshold is not met, record the outcome as `weak` or `blocked` and pivot to the submission package or another higher-confidence improvement.

## Known Context

- Bloomreach project slug: `sleepy-goose`
- Existing Growth Loop action: `Booking-step recovery brief`
- Existing app-owned proof: one tracked content item, one booking, one paid invoice, and `$195.00`
- Existing Loomi proof: authenticated Marketing and Analytics MCP showed a rich ecommerce event schema, including cart, checkout, purchase, campaign, retargeting, and product-view events.
- Existing Conversation MCP proof: connected successfully, but exposed catalog-proxy signals rather than support, checkout, refund, booking, or payment-failure telemetry.
- Evidence boundary: Engagement and Storefront observations can explain demo context and recovery opportunity, but app-owned booking, invoice, and payment records remain paid truth.

## Safety Rules

- Do not commit raw private access URLs, credentials, browser session data, callback URLs, cookies, auth artifacts, or secrets.
- Do not paste raw customer records, raw event payloads, email addresses, order details, or personally identifying customer details into this file.
- Do not store screenshots in tracked docs unless they are reviewed, cropped, and scrubbed.
- Prefer aggregate, paraphrased, or visibly public storefront facts.
- Do not click send, publish, save, export, create, delete, checkout, refund, disconnect, or mutate anything during this proof.
- Do not claim Engagement or Storefront observations prove revenue, attribution, causality, or lift.
- Do not claim `/app/growth-loop` made a live Engagement or Storefront call.

## Browser Setup Checklist

Use the browser and the operator-provided sandbox links. Do not add these links to tracked files.

1. Open the Engagement sandbox access URL in the browser.
2. Open the Storefront sandbox access URL in the browser.
3. Authenticate only in the browser if prompted.
4. Keep the proof read-only.
5. Record only sanitized facts in the tables below.
6. If a surface asks you to create, save, send, publish, checkout, or configure anything, stop and record that the action was intentionally skipped.

## Browser Proof Steps

### 1. Engagement Project Scope

Goal: confirm whether Engagement shows a safe project/account/workspace label or other non-sensitive context that links the sandbox to `sleepy-goose`.

Look for:

- visible project, account, workspace, or site name
- safe project slug or label
- existing dashboards, scenarios, campaigns, segmentations, recommendations, catalogs, or event schema areas
- ecommerce schema or activity names that match the Loomi MCP proof

Record only names or high-level counts that are safe to display.

### 2. Engagement Growth-Loop Relevance

Goal: identify whether Engagement has read-only evidence that supports the recovery-loop narrative.

Look for:

- cart, checkout, product view, purchase, campaign, retargeting, or consent event concepts
- segment-building affordances relevant to cart or booking-step recovery
- campaign or retargeting surfaces that would be used after human review
- analytics/funnel/reporting surfaces that could evaluate paid conversion later

Do not create or save any segment, campaign, report, recommendation, or automation.

### 3. Storefront Catalog Proof

Goal: confirm whether the storefront exposes safe visible product/category facts that can make the Growth Loop demo feel connected to a real commerce surface.

Look for:

- visible storefront name or slug
- product collections, categories, product names, brands, prices, sale labels, availability labels, or navigation structure
- cart and checkout entry points, without completing checkout
- visible product/category names that correspond to schema fields from the Loomi proof

Record public or non-sensitive visible facts only.

### 4. Cross-System Relationship

Goal: decide whether Engagement and Storefront together produce a stronger proof chain than the app alone.

Look for at least three facts that connect:

- Storefront catalog or shopping behavior
- Engagement/Loomi event or activation surfaces
- app-owned paid proof and review-only Growth Loop action

Example acceptable facts:

- Storefront has visible product/category structure, and Engagement/Loomi has matching product/category event concepts.
- Engagement has segment-building or campaign surfaces that match the Bloomreach-ready recipe, but no segment is created.
- Storefront exposes cart/checkout flow context, and Growth Loop uses app-owned invoice/payment records as the paid outcome boundary.

### 5. Boundary Confirmation

Goal: prove the demo remains honest.

Confirm:

- no campaign was sent
- no segment, campaign, scenario, recommendation, report, or storefront content was created or saved
- no checkout, payment, refund, or provider operation was performed
- no customer PII or raw event payload was copied into tracked docs
- no live app integration with Engagement or Storefront was added

## Proof Result Template

Fill this section after the manual browser proof.

### Run Metadata

- Date: 2026-05-28
- Operator: Eric Mariasis
- Engagement access: browser/manual only
- Storefront access: browser/manual only
- Raw private URLs stored in tracked docs: no
- Auth status: Engagement authenticated in browser; Storefront loaded in browser
- Screenshots stored in tracked docs: no
- Mutation status: no external mutations performed

### Surface Inventory

| Surface | Safe Observed Fact | Why It Matters | Boundary |
| --- | --- | --- | --- |
| Engagement | Authenticated Bloomreach Engagement project shows `Hackathon Org`, `Hackathon Workspace`, and `sleepy-goose`. Left navigation includes `Campaigns`, `Analyses`, `Data & Assets`, `Initiatives`, and `Use Case Center`. `Data & Assets` exposes `Customers`, `Catalogs`, `Tag manager`, `Data manager`, `Metrics`, `Imports`, `Integrations`, `Managed endpoints`, and `API trigger`. The Customers view shows an aggregate customer table and filter builder. Data manager exposes customer properties plus ecommerce/recovery-relevant events such as `view_category`, `view_item`, `cart_update`, `checkout`, `campaign`, `purchase`, `purchase_item`, `loyalty_update`, `support_ticket`, `return`, `registration`, and `retargeting`. Analyses exposes `Trends`, `Funnels`, `Reports`, `Retentions`, `Segmentations`, `Flows`, `Geo analyses`, `Predictions`, and `SQL Reports` | Confirms the sandbox has the activation, analysis, data, customer, catalog, integration, event-schema, segmentation, funnel, and reporting surfaces needed to manually recreate and evaluate a recovery loop | Diagnostic/demo context only; no customer identities, raw records, filters, segments, campaigns, dashboards, exports, analyses, or mutations recorded |
| Storefront | Visible brand `Pacific Apparel`; categories include `Women`, `Men`, `Accessories`, and `Outerwear`; cart entry point is visible. The `Bags` category loads `Handbags` results with product grids, subcategories, sale/price facets, pagination, and visible discount labels | Shows a real commerce surface with catalog, cart, product-listing, discount, and price-filter context for the recovery-loop story | Public/sandbox storefront context only; no checkout or payment performed |

### Cross-System Facts

| Fact | Source | Safe Evidence | Why It Matters | Growth Loop Use | Boundary |
| --- | --- | --- | --- | --- | --- |
| Storefront catalog and cart context | Storefront | `Pacific Apparel` storefront loads with product categories and a visible cart entry point | Establishes that the sandbox includes a real shopping surface where cart or checkout recovery makes sense | Supports the Growth Loop narrative that recovery actions are tied to commerce behavior, while app-owned records remain paid truth | Diagnostic/demo context only; no checkout, payment, or storefront mutation performed |
| Storefront merchandising and offer context | Storefront | `Bags` category loads `Handbags` results, subcategories such as `backpacks&totes` and `purses`, product grids, price/sale filters, pagination, and visible discount labels | Establishes that recovery messaging can be grounded in catalog, price, and offer context instead of generic follow-up copy | Supports the Growth Loop review packet with safe offer-aware and category-aware copy inputs, while app-owned paid outcomes remain the measurement source | Diagnostic/demo context only; no add-to-cart, checkout, payment, or storefront mutation performed |
| Engagement activation and analysis surfaces | Engagement | `sleepy-goose` project in `Hackathon Workspace` exposes campaign, analysis, customer, catalog, data-manager, metric, integration, and API-trigger surfaces, plus customer filtering | Establishes that the sandbox has the read-side data and activation areas needed to recreate the Growth Loop segment/action recipe after human review | Supports the Growth Loop narrative that the app can produce a review-ready recovery plan that maps to Bloomreach surfaces, while no Bloomreach object is created by this app | Diagnostic/demo context only; no customer PII copied, no filter saved, no segment/campaign/dashboard/export created |
| Engagement ecommerce recovery event schema | Engagement | Data manager lists recovery-relevant event names including `cart_update`, `checkout`, `view_item`, `view_category`, `campaign`, `purchase`, `purchase_item`, `support_ticket`, `return`, and `retargeting` | Establishes that the sandbox has first-class event concepts for the exact cart/checkout/recovery loop used in the Growth Loop demo | Strengthens the proof chain from Storefront shopping behavior to Engagement event schema to app-owned paid outcome measurement | Diagnostic/demo context only; no event payloads copied, no schema changes saved, no live app query performed |
| Engagement measurement surfaces | Engagement | Analyses navigation exposes `Trends`, `Funnels`, `Reports`, `Retentions`, `Segmentations`, `Flows`, `Geo analyses`, `Predictions`, and `SQL Reports` | Establishes that the sandbox has measurement and segmentation surfaces for evaluating a recovery loop after human-reviewed setup | Supports the Growth Loop measurement plan: future success can be evaluated in analysis/funnel/reporting surfaces while the app still uses app-owned invoices and payment records as paid truth | Diagnostic/demo context only; no analysis, report, funnel, segment, SQL report, dashboard, or flow created |

### Decision

Choose one after the proof:

- `pass`: at least three safe cross-system facts found; plan a compact app-facing Sandbox Proof Artifact next
- `weak`: surfaces work, but facts do not materially strengthen the Growth Loop demo; pivot to submission package or another higher-confidence improvement
- `blocked`: auth, access, or data visibility blocked; record blocker and pivot

Decision: `pass`

### Sanitized Notes

- Manual browser proof completed on 2026-05-28.
- Engagement Customers view displayed raw customer rows; this packet records only aggregate/navigation facts and does not copy customer identities.
- Engagement Data manager event proof is recorded as event names only; no raw event payloads or customer-level rows are copied.
- Engagement Analyses proof is recorded as navigation capability only; no analysis, report, funnel, segment, flow, prediction, or SQL report is created.
- Operator confirmed no Engagement save/create/add/export/send action was used, no segment/campaign/dashboard/report/flow/funnel/catalog/storefront object was created or saved, no Storefront add-to-cart/checkout/payment action was used, and no customer emails or raw customer/event records were copied into this packet.
- No raw private URLs, credentials, session data, callback URLs, cookies, auth artifacts, raw customer records, raw event payloads, or customer PII should be stored here.
- No external mutations should be performed during this proof.

## What This Proof Should Enable

If successful, the next app story can add a compact paid-result-only Sandbox Proof Artifact to `/app/growth-loop`. That artifact should explain how Storefront shopping context, Engagement/Loomi diagnostic surfaces, and app-owned paid proof fit together without claiming live mutation, live integration, causality, or measured lift.

If weak or blocked, the next story should avoid adding more UI weight and focus on submission packaging, demo narration, or another higher-confidence proof path.
