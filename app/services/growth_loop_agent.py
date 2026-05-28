from dataclasses import dataclass


GROWTH_LOOP_STAGE_SETUP_INCOMPLETE = "setup_incomplete"
GROWTH_LOOP_STAGE_BILLABLE_NO_TRACKED_CONTENT = "billable_no_tracked_content"
GROWTH_LOOP_STAGE_TRACKED_NO_BOOKINGS = "tracked_no_bookings"
GROWTH_LOOP_STAGE_BOOKINGS_NO_PAID_RESULTS = "bookings_no_paid_results"
GROWTH_LOOP_STAGE_PAID_RESULT_EXISTS = "paid_result_exists"


@dataclass(frozen=True)
class GrowthLoopWorkspaceEvidence:
    billing_connected: bool
    billable_now: bool
    booking_links_count: int
    billing_ready_count: int
    tracked_content_count: int
    booking_count: int
    paid_invoice_count: int
    paid_revenue_cents: int
    billing_provider: str | None = None


@dataclass(frozen=True)
class GrowthLoopEvidenceItem:
    label: str
    value: str
    detail: str


@dataclass(frozen=True)
class GrowthLoopSchemaOpportunity:
    source_label: str
    source_status_label: str
    source_summary: str
    project_name: str
    project_id: str
    opportunity_title: str
    opportunity_summary: str
    app_bridge_summary: str
    required_segment: tuple[str, ...]
    recommended_action: tuple[str, ...]
    proof_evidence: tuple[str, ...]
    event_properties: tuple[str, ...]
    limitations: tuple[str, ...]


@dataclass(frozen=True)
class GrowthLoopReviewableActionBrief:
    title: str
    summary: str
    target_segment: tuple[str, ...]
    message_outline: tuple[str, ...]
    bloomreach_next_step: tuple[str, ...]
    success_evidence: tuple[str, ...]
    diagnostic_signals: tuple[str, ...]
    copy_ready_text: str
    limitations: tuple[str, ...]


@dataclass(frozen=True)
class LoomiDiagnosticContext:
    source_label: str
    source_kind: str
    segments: tuple[str, ...]
    predictions: tuple[str, ...]
    recommendations: tuple[str, ...]
    analytics: tuple[str, ...]
    limitations: tuple[str, ...]
    source_status_label: str = "Loomi fixture fallback"
    source_status_kind: str = "fixture_fallback"
    source_status_detail: str = (
        "Live Loomi MCP is not configured for this request, so fixture diagnostics are shown."
    )


@dataclass(frozen=True)
class GrowthLoopActionBrief:
    stage: str
    diagnosis_title: str
    diagnosis_summary: str
    next_action_title: str
    next_action_summary: str
    prepared_action_title: str
    prepared_action_body: str
    app_evidence: tuple[GrowthLoopEvidenceItem, ...]
    loomi_context: LoomiDiagnosticContext
    schema_opportunity: GrowthLoopSchemaOpportunity
    reviewable_action: GrowthLoopReviewableActionBrief | None
    confidence_label: str
    confidence_summary: str
    limitations: tuple[str, ...]
    human_review_note: str


def build_fixture_loomi_diagnostic_context(
    *,
    source_status_detail: str | None = None,
) -> LoomiDiagnosticContext:
    return LoomiDiagnosticContext(
        source_label="Loomi fixture diagnostics",
        source_kind="diagnostic_fixture",
        segments=(
            "High-intent tutoring prospects reviewing career-change guidance",
            "Booking-page visitors who have not produced a paid result yet",
        ),
        predictions=(
            "Next-action fit is strongest for prospects who already reached a booking step",
            "Paid-outcome confidence stays low until app-owned invoice/payment evidence exists",
        ),
        recommendations=(
            "Prepare one reviewable follow-up brief before any creator sends or publishes anything",
            "Use the paid-result evidence from this app as the outcome boundary",
        ),
        analytics=(
            "Fixture trend: engagement context is available for diagnosis, not for revenue truth",
            "Fixture trend: booking activity is the highest-leverage handoff point to inspect",
        ),
        limitations=(
            "Loomi context is diagnostic fixture data in this slice.",
            "It does not count revenue, prove causality, or replace app-owned booking and payment records.",
        ),
        source_status_detail=(
            source_status_detail
            or "Live Loomi MCP is not configured for this request, so fixture diagnostics are shown."
        ),
    )


def build_growth_loop_action_brief(
    *,
    evidence: GrowthLoopWorkspaceEvidence,
    loomi_context: LoomiDiagnosticContext | None = None,
) -> GrowthLoopActionBrief:
    context = loomi_context or build_fixture_loomi_diagnostic_context()
    stage = _classify_growth_loop_stage(evidence)
    stage_copy = _stage_copy(stage)

    return GrowthLoopActionBrief(
        stage=stage,
        diagnosis_title=stage_copy["diagnosis_title"],
        diagnosis_summary=stage_copy["diagnosis_summary"],
        next_action_title=stage_copy["next_action_title"],
        next_action_summary=stage_copy["next_action_summary"],
        prepared_action_title=stage_copy["prepared_action_title"],
        prepared_action_body=stage_copy["prepared_action_body"],
        app_evidence=_app_evidence_items(evidence),
        loomi_context=context,
        schema_opportunity=build_sleepy_goose_schema_opportunity(),
        reviewable_action=_build_reviewable_action_brief(stage),
        confidence_label=stage_copy["confidence_label"],
        confidence_summary=stage_copy["confidence_summary"],
        limitations=(
            "Attribution remains last-touch through the stored booking/content relationship.",
            "Revenue remains canonical invoice/payment truth from this app.",
            "Loomi diagnostics are context for review, not a second paid-result ledger.",
            "This slice prepares an action for human review and does not execute external mutations.",
        ),
        human_review_note=(
            "Review this action before sending, publishing, or changing any external system."
        ),
    )


def _build_reviewable_action_brief(
    stage: str,
) -> GrowthLoopReviewableActionBrief | None:
    if stage != GROWTH_LOOP_STAGE_PAID_RESULT_EXISTS:
        return None

    return GrowthLoopReviewableActionBrief(
        title="Reviewable recovery brief",
        summary=(
            "Prepare one booking-step recovery brief from the schema-backed cart-abandon "
            "opportunity and the paid-result path already proven in this workspace."
        ),
        target_segment=(
            "Prospects who reached a booking or checkout-like step but have not produced a later paid result.",
            "Prioritize high-intent activity that resembles non-empty carts, checkout starts, or product/category views.",
            "Exclude anyone who already has a later app-owned paid invoice or payment-backed result.",
        ),
        message_outline=(
            "Open with a helpful reminder that the booking step was not finished.",
            "Reference the last known interest area in plain tutor-safe language.",
            "Invite the prospect back to the same measured booking path so the next outcome can be reviewed.",
        ),
        bloomreach_next_step=(
            "Draft a segment spec using cart_update, checkout, view_item, and purchase exclusion logic.",
            "Keep the segment as a human-reviewed recipe until someone recreates it inside Bloomreach.",
            "Use campaign and retargeting events only as diagnostic engagement signals after review.",
        ),
        success_evidence=(
            "Primary proof is later app-owned paid conversion lift inside the reviewed target segment.",
            "Count paid success only through stored booking, invoice, and payment-backed records in this app.",
            "Compare recovered prospects against a holdout or non-targeted group before claiming improvement.",
        ),
        diagnostic_signals=(
            "campaign.status, campaign.url, and campaign.action_type can show message engagement.",
            "retargeting.audience, retargeting.platform, and retargeting.action can show audience exposure.",
            "These signals explain engagement context; they do not count revenue or prove causality.",
        ),
        copy_ready_text=(
            "Draft recovery brief: Review prospects who reached the booking step but did not finish payment. "
            "Prepare a short reminder tied to their last known interest area, send it only after human review, "
            "and measure success through later app-owned paid invoices and payment-backed records. "
            "Use Bloomreach campaign and retargeting events as diagnostic engagement context only."
        ),
        limitations=(
            "Prepared for human review only; this app does not send the recovery message.",
            "This draft does not mutate Bloomreach or create a saved segment, campaign, or recommendation.",
            "It does not count revenue, prove causality, or replace app-owned booking, invoice, and payment records.",
        ),
    )


def build_sleepy_goose_schema_opportunity() -> GrowthLoopSchemaOpportunity:
    return GrowthLoopSchemaOpportunity(
        source_label="Live Loomi schema proof",
        source_status_label="Verified via Cursor MCP",
        source_summary=(
            "Cursor authenticated to the live Loomi MCP server and inspected the sleepy-goose "
            "project. The sandbox has no saved segmentations, recommendations, reports, funnels, "
            "scenarios, or campaigns yet, but its event schema is rich enough to plan a reviewed "
            "growth loop."
        ),
        project_name="sleepy-goose",
        project_id="b15c09b0-5469-11f1-b333-862b79b06b65",
        opportunity_title="Cart-abandon recover & convert",
        opportunity_summary=(
            "Find customers with meaningful cart activity who have not completed a later purchase, "
            "then prepare a reviewed recovery action tied to the last known cart context."
        ),
        app_bridge_summary=(
            "For this tutoring app, the same pattern maps to booking-step recovery: Loomi schema "
            "intelligence suggests the recovery loop, while app-owned bookings, invoices, and "
            "payments decide whether the loop later produced paid truth."
        ),
        required_segment=(
            "Include customers with cart_update activity where total_quantity is greater than zero.",
            "Prioritize high-intent carts using total_price, checkout product IDs, or product/category views.",
            "Exclude customers with a later completed purchase inside the chosen recovery window.",
        ),
        recommended_action=(
            "Prepare a message-based recovery flow for human review before sending.",
            "Reference the last cart items or categories so the recovery message stays relevant.",
            "Track downstream engagement with campaign events and retargeting audience actions.",
        ),
        proof_evidence=(
            "Completed purchase events after the recovery send increase inside the target segment.",
            "Revenue lift is measured with purchase total_price and purchase_item quantity/product IDs.",
            "Campaign opens, clicks, URLs, and retargeting actions provide diagnostic engagement context.",
        ),
        event_properties=(
            "cart_update.action",
            "cart_update.total_quantity",
            "cart_update.total_price",
            "checkout.total_price",
            "checkout.product_ids",
            "view_item.product_id",
            "view_item.category_level_1",
            "view_item.category_level_2",
            "view_item.category_level_3",
            "purchase.purchase_status",
            "purchase.total_price",
            "purchase_item.quantity",
            "purchase_item.product_id",
            "campaign.campaign_name",
            "campaign.action_type",
            "campaign.status",
            "campaign.url",
            "retargeting.audience",
            "retargeting.platform",
            "retargeting.action",
        ),
        limitations=(
            "This is a deterministic blueprint from verified live Loomi schema proof, not a live page-load MCP call.",
            "It does not send campaigns, mutate Bloomreach, or create a saved Loomi segmentation.",
            "It does not count revenue, prove causality, or replace app-owned booking, invoice, and payment records.",
        ),
    )


def _classify_growth_loop_stage(evidence: GrowthLoopWorkspaceEvidence) -> str:
    if evidence.paid_invoice_count > 0:
        return GROWTH_LOOP_STAGE_PAID_RESULT_EXISTS
    if evidence.booking_count > 0:
        return GROWTH_LOOP_STAGE_BOOKINGS_NO_PAID_RESULTS
    if evidence.tracked_content_count > 0:
        return GROWTH_LOOP_STAGE_TRACKED_NO_BOOKINGS
    if evidence.billable_now:
        return GROWTH_LOOP_STAGE_BILLABLE_NO_TRACKED_CONTENT
    return GROWTH_LOOP_STAGE_SETUP_INCOMPLETE


def _app_evidence_items(
    evidence: GrowthLoopWorkspaceEvidence,
) -> tuple[GrowthLoopEvidenceItem, ...]:
    return (
        GrowthLoopEvidenceItem(
            label="Tracked content",
            value=_count_copy(evidence.tracked_content_count, "content item"),
            detail="App-owned content records define what can receive booking attribution.",
        ),
        GrowthLoopEvidenceItem(
            label="Tracked bookings",
            value=_count_copy(evidence.booking_count, "booking"),
            detail="Bookings count only when this workspace recorded a booking path.",
        ),
        GrowthLoopEvidenceItem(
            label="Canonical invoices",
            value=_count_copy(evidence.paid_invoice_count, "paid invoice"),
            detail="Paid result counts come from stored invoice records in this app.",
        ),
        GrowthLoopEvidenceItem(
            label="Canonical payment truth",
            value=_money_copy(evidence.paid_revenue_cents),
            detail="Revenue is app-owned invoice/payment evidence, not Loomi diagnostic context.",
        ),
    )


def _stage_copy(stage: str) -> dict[str, str]:
    if stage == GROWTH_LOOP_STAGE_PAID_RESULT_EXISTS:
        return {
            "diagnosis_title": "Paid proof exists; choose the next reviewed action.",
            "diagnosis_summary": (
                "This workspace already has app-owned paid-result evidence. Use Loomi context "
                "to frame the next reviewable action, while keeping the paid truth tied to "
                "stored invoices and payment records."
            ),
            "next_action_title": "Prepare one follow-up brief from the proven path",
            "next_action_summary": (
                "Review the content and booking path that already produced a paid result, then "
                "prepare a narrow follow-up for a similar diagnostic segment."
            ),
            "prepared_action_title": "Reviewable follow-up brief",
            "prepared_action_body": (
                "Draft a short follow-up for prospects who reached a booking step but did not "
                "complete payment. Cite the existing paid-result path as evidence that this "
                "workflow can produce paid students, without making a stronger claim than the app supports."
            ),
            "confidence_label": "Higher confidence",
            "confidence_summary": (
                "Confidence is higher because the recommendation can reference canonical paid "
                "invoice/payment evidence from this workspace."
            ),
        }
    if stage == GROWTH_LOOP_STAGE_BOOKINGS_NO_PAID_RESULTS:
        return {
            "diagnosis_title": "Bookings exist, but paid proof has not landed yet.",
            "diagnosis_summary": (
                "The workspace has booking activity, but no canonical paid invoice is counted. "
                "The next action should inspect the booking-to-paid handoff instead of widening "
                "into new campaign work."
            ),
            "next_action_title": "Review booking-to-paid follow-up",
            "next_action_summary": (
                "Prepare one follow-up action for booked prospects and keep it under human review."
            ),
            "prepared_action_title": "Booking follow-up brief",
            "prepared_action_body": (
                "List the booked prospects or booking path that still lacks paid evidence, then "
                "prepare a reviewed follow-up reminder focused on completing the paid step."
            ),
            "confidence_label": "Medium confidence",
            "confidence_summary": (
                "Confidence is medium because app-owned bookings exist, but revenue is not counted yet."
            ),
        }
    if stage == GROWTH_LOOP_STAGE_TRACKED_NO_BOOKINGS:
        return {
            "diagnosis_title": "Tracked content exists, but bookings have not started.",
            "diagnosis_summary": (
                "The workspace can attribute content, but there is no recorded booking activity yet. "
                "The next action should drive one measurable booking path."
            ),
            "next_action_title": "Promote one tracked booking path",
            "next_action_summary": (
                "Choose one tracked content item and one booking link to review before sharing again."
            ),
            "prepared_action_title": "Tracked content handoff brief",
            "prepared_action_body": (
                "Prepare a short note that points prospects to the existing tracked booking link. "
                "Review it before posting so the next result can be measured from content to booking."
            ),
            "confidence_label": "Medium-low confidence",
            "confidence_summary": (
                "Confidence is limited because attribution setup exists, but no booking evidence has landed."
            ),
        }
    if stage == GROWTH_LOOP_STAGE_BILLABLE_NO_TRACKED_CONTENT:
        return {
            "diagnosis_title": "Billing is ready, but no tracked content exists.",
            "diagnosis_summary": (
                "The workspace can support paid proof, but it has no tracked content path to test yet."
            ),
            "next_action_title": "Add one tracked content item",
            "next_action_summary": (
                "Create one tracked content path tied to a billable booking link before reviewing growth actions."
            ),
            "prepared_action_title": "First tracked-link brief",
            "prepared_action_body": (
                "Pick one existing post, email, or profile link and attach it to a billable booking link. "
                "Use that as the first measured path before making broader recommendations."
            ),
            "confidence_label": "Low confidence",
            "confidence_summary": (
                "Confidence is low because no app-owned content path has been measured yet."
            ),
        }
    return {
        "diagnosis_title": "Setup is not ready for paid-outcome diagnosis yet.",
        "diagnosis_summary": (
            "The workspace does not yet have enough billable tracking setup to support a paid-result "
            "growth-loop recommendation."
        ),
        "next_action_title": "Finish billable tracking setup",
        "next_action_summary": (
            "Connect billing and save a booking link with billing defaults before relying on agent guidance."
        ),
        "prepared_action_title": "Setup completion brief",
        "prepared_action_body": (
            "Complete the billing and booking-link setup first. After the workspace can record a "
            "billable booking path, return here for a reviewable growth-loop action."
        ),
        "confidence_label": "Setup confidence only",
        "confidence_summary": (
            "Confidence only covers the setup diagnosis because paid-result evidence is not available."
        ),
    }


def _count_copy(count: int, singular: str) -> str:
    if count == 1:
        return f"1 {singular}"
    return f"{count} {singular}s"


def _money_copy(amount_cents: int) -> str:
    dollars = amount_cents / 100
    return f"${dollars:,.2f}"
