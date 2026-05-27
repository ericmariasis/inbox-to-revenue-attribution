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
