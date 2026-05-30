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
class GrowthLoopDecisionCandidate:
    title: str
    status_label: str
    score: int
    max_score: int
    summary: str
    criteria: tuple[str, ...]
    outcome: str
    boundary: str


@dataclass(frozen=True)
class GrowthLoopDecisionTrace:
    title: str
    summary: str
    guardrail_summary: str
    scoring_criteria: tuple[str, ...]
    evidence_chain: tuple[str, ...]
    candidates: tuple[GrowthLoopDecisionCandidate, ...]


@dataclass(frozen=True)
class GrowthLoopSegmentRecipe:
    title: str
    summary: str
    include_rules: tuple[str, ...]
    exclude_rules: tuple[str, ...]
    recovery_window: tuple[str, ...]
    message_variables: tuple[str, ...]
    measurement_plan: tuple[str, ...]
    conversation_mcp_note: str
    limitations: tuple[str, ...]


@dataclass(frozen=True)
class GrowthLoopMeasurementCard:
    label: str
    title: str
    detail: str


@dataclass(frozen=True)
class GrowthLoopMeasurementPlan:
    title: str
    summary: str
    cards: tuple[GrowthLoopMeasurementCard, ...]
    limitations: tuple[str, ...]


@dataclass(frozen=True)
class GrowthLoopAgentConsoleStep:
    label: str
    title: str
    detail: str
    status_label: str


@dataclass(frozen=True)
class GrowthLoopGuidedRunStep:
    label: str
    title: str
    summary: str
    evidence_label: str
    evidence_detail: str
    target_anchor: str


@dataclass(frozen=True)
class GrowthLoopGuidedRun:
    title: str
    summary: str
    primary_action_label: str
    completion_title: str
    completion_summary: str
    steps: tuple[GrowthLoopGuidedRunStep, ...]
    boundaries: tuple[str, ...]


@dataclass(frozen=True)
class GrowthLoopCapabilitySignal:
    label: str
    value: str
    detail: str


@dataclass(frozen=True)
class GrowthLoopReviewPacket:
    title: str
    summary: str
    selected_action: str
    segment_summary: str
    measurement_summary: str
    proof_chain: tuple[str, ...]
    boundaries: tuple[str, ...]


@dataclass(frozen=True)
class GrowthLoopSandboxProofCard:
    label: str
    title: str
    detail: str


@dataclass(frozen=True)
class GrowthLoopSandboxProof:
    title: str
    summary: str
    status_label: str
    cards: tuple[GrowthLoopSandboxProofCard, ...]
    proof_chain: tuple[str, ...]
    boundaries: tuple[str, ...]


@dataclass(frozen=True)
class GrowthLoopRecordedBloomreachSegmentProof:
    segment_name: str
    segment_id: str
    project_name: str = "sleepy-goose"
    workspace_name: str = "Hackathon Workspace"
    created_via: str = "Bloomreach Engagement UI"
    status_label: str = "Created in Engagement UI"


@dataclass(frozen=True)
class GrowthLoopBloomreachObjectProofCard:
    label: str
    title: str
    detail: str


@dataclass(frozen=True)
class GrowthLoopBloomreachObjectProof:
    title: str
    summary: str
    status_label: str
    object_type: str
    object_name: str
    object_id: str
    project_name: str
    workspace_name: str
    created_via: str
    cards: tuple[GrowthLoopBloomreachObjectProofCard, ...]
    proof_chain: tuple[str, ...]
    boundaries: tuple[str, ...]


@dataclass(frozen=True)
class GrowthLoopAgentConsole:
    title: str
    summary: str
    primary_action_label: str
    steps: tuple[GrowthLoopAgentConsoleStep, ...]
    guided_run: GrowthLoopGuidedRun
    capability_signals: tuple[GrowthLoopCapabilitySignal, ...]
    review_packet: GrowthLoopReviewPacket


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
    decision_trace: GrowthLoopDecisionTrace | None
    segment_recipe: GrowthLoopSegmentRecipe | None
    measurement_plan: GrowthLoopMeasurementPlan | None
    sandbox_proof: GrowthLoopSandboxProof | None
    bloomreach_object_proof: GrowthLoopBloomreachObjectProof | None
    agent_console: GrowthLoopAgentConsole | None
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
    recorded_bloomreach_segment_proof: GrowthLoopRecordedBloomreachSegmentProof | None = None,
) -> GrowthLoopActionBrief:
    context = loomi_context or build_fixture_loomi_diagnostic_context()
    stage = _classify_growth_loop_stage(evidence)
    stage_copy = _stage_copy(stage)
    bloomreach_object_proof = _build_bloomreach_object_proof(
        stage,
        recorded_bloomreach_segment_proof,
    )

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
        reviewable_action=_build_reviewable_action_brief(stage, bloomreach_object_proof),
        decision_trace=_build_decision_trace(stage, evidence, bloomreach_object_proof),
        segment_recipe=_build_segment_recipe(stage, bloomreach_object_proof),
        measurement_plan=_build_measurement_plan(stage),
        sandbox_proof=_build_sandbox_proof(stage, evidence),
        bloomreach_object_proof=bloomreach_object_proof,
        agent_console=_build_agent_console(stage, evidence, bloomreach_object_proof),
        confidence_label=stage_copy["confidence_label"],
        confidence_summary=stage_copy["confidence_summary"],
        limitations=(
            "Attribution remains last-touch through the stored booking/content relationship.",
            "Revenue remains canonical invoice/payment truth from this app.",
            "Loomi diagnostics are context for review, not a second paid-result ledger.",
            "This page prepares an action for human review and does not execute external mutations on load.",
        ),
        human_review_note=(
            "Review this action before sending, publishing, or changing any external system."
        ),
    )


def _build_bloomreach_object_proof(
    stage: str,
    recorded_proof: GrowthLoopRecordedBloomreachSegmentProof | None,
) -> GrowthLoopBloomreachObjectProof | None:
    if stage != GROWTH_LOOP_STAGE_PAID_RESULT_EXISTS or recorded_proof is None:
        return None

    segment_name = recorded_proof.segment_name.strip()
    segment_id = recorded_proof.segment_id.strip()
    if not segment_name or not segment_id:
        return None

    project_name = recorded_proof.project_name.strip() or "sleepy-goose"
    workspace_name = recorded_proof.workspace_name.strip() or "Hackathon Workspace"
    created_via = recorded_proof.created_via.strip() or "Bloomreach Engagement UI"
    status_label = recorded_proof.status_label.strip() or "Created in Engagement UI"

    return GrowthLoopBloomreachObjectProof(
        title="Bloomreach saved segment proof",
        summary=(
            f"This records one real saved segment created in Bloomreach through {created_via}. "
            "This app displays sanitized object metadata as proof; it does not create or "
            "mutate Bloomreach on page load."
        ),
        status_label=status_label,
        object_type="Saved segment",
        object_name=segment_name,
        object_id=segment_id,
        project_name=project_name,
        workspace_name=workspace_name,
        created_via=created_via,
        cards=(
            GrowthLoopBloomreachObjectProofCard(
                label="Saved segment",
                title=segment_name,
                detail=f"Recorded {segment_id} as the Bloomreach segment proof object.",
            ),
            GrowthLoopBloomreachObjectProofCard(
                label="Sandbox location",
                title=f"{project_name} / {workspace_name}",
                detail=f"Created through {created_via} after the agent selected the recovery loop.",
            ),
            GrowthLoopBloomreachObjectProofCard(
                label="Runtime boundary",
                title="Displayed, not created here",
                detail=(
                    "The app shows recorded metadata only; no create, update, delete, send, "
                    "export, or checkout action runs from this page."
                ),
            ),
        ),
        proof_chain=(
            f"{created_via} created saved segment {segment_name} ({segment_id}) in {project_name}.",
            "The app displays the recorded object metadata as review proof, not as a page-load mutation.",
            "Campaign activation remains gated; no send, export, checkout, or payment action is triggered.",
            "App-owned invoice and payment records remain the paid-result truth.",
        ),
        boundaries=(
            "No campaign, flow, send, export, checkout, payment, or Storefront mutation is triggered by this proof.",
            "This page does not create, update, or delete Bloomreach objects on load.",
            "The created segment does not count revenue or prove lift/causality.",
            "App-owned invoice and payment records remain paid truth.",
        ),
    )


def _build_segment_recipe(
    stage: str,
    bloomreach_object_proof: GrowthLoopBloomreachObjectProof | None,
) -> GrowthLoopSegmentRecipe | None:
    if stage != GROWTH_LOOP_STAGE_PAID_RESULT_EXISTS:
        return None

    if bloomreach_object_proof is None:
        summary = (
            "Use this as the reviewed recipe a marketer can recreate in Bloomreach. "
            "It translates the verified cart-abandon schema into this app's booking-step "
            "recovery analogue without creating or mutating any Bloomreach object."
        )
        limitations = (
            "Review-only recipe; this app does not create a saved Bloomreach segment, campaign, or recommendation.",
            "No campaign is sent and no external system is mutated from this page.",
            "Loomi and Conversation signals do not count revenue, prove causality, or replace app-owned booking, invoice, and payment records.",
        )
    else:
        summary = (
            "Use this as the reviewed recipe behind the recorded Bloomreach saved segment proof. "
            "It translates the verified cart-abandon schema into this app's booking-step "
            "recovery analogue while keeping activation behind human review."
        )
        limitations = (
            f"The recorded saved segment was created through {bloomreach_object_proof.created_via}; this page does not create or update it on load.",
            "No campaign is sent and no external system is mutated from this page.",
            "Loomi, Conversation, and saved-segment proof do not count revenue, prove causality, or replace app-owned booking, invoice, and payment records.",
        )

    return GrowthLoopSegmentRecipe(
        title="Bloomreach-ready segment recipe",
        summary=summary,
        include_rules=(
            "Include people with cart_update activity where total_quantity is greater than zero.",
            "Prioritize checkout starts, product IDs, total cart value, or view_item category activity when available.",
            "For this app, map the same pattern to prospects who reached a booking or checkout-like step from tracked content.",
        ),
        exclude_rules=(
            "Exclude anyone with a later completed purchase event inside the recovery window.",
            "Exclude anyone with a later app-owned paid invoice or payment-backed result in this workspace.",
            "Exclude suppressed, unsubscribed, or otherwise ineligible contacts before recreating the segment in Bloomreach.",
        ),
        recovery_window=(
            "Default to a 24-hour recovery window after the last qualifying cart, checkout, or booking-step activity.",
            "Keep the first reviewed slice narrow; widen the window only after comparing paid results and engagement quality.",
        ),
        message_variables=(
            "Last product ID, category, or app interest area.",
            "Last cart value, total quantity, or booking-step context where available.",
            "Return path to the same measured booking or checkout flow so later outcomes remain reviewable.",
        ),
        measurement_plan=(
            "Measure success through later app-owned paid invoices and payment-backed records.",
            "Compare targeted prospects against a holdout or non-targeted group before claiming improvement.",
            "Use campaign.status, campaign.url, retargeting.audience, and retargeting.action as diagnostic engagement context only.",
        ),
        conversation_mcp_note=(
            "Story 131 tested Conversation MCP and deferred it for this recipe: the server connected, "
            "but exposed catalog-proxy signals rather than support, checkout, booking, refund, or payment-failure telemetry."
        ),
        limitations=limitations,
    )


def _build_decision_trace(
    stage: str,
    evidence: GrowthLoopWorkspaceEvidence,
    bloomreach_object_proof: GrowthLoopBloomreachObjectProof | None,
) -> GrowthLoopDecisionTrace | None:
    if stage != GROWTH_LOOP_STAGE_PAID_RESULT_EXISTS:
        return None

    if bloomreach_object_proof is None:
        guardrail_summary = (
            "Rule-backed trace only: no live LLM call is required, no campaign is sent, "
            "no Bloomreach object is mutated, and Loomi diagnostics do not become paid truth."
        )
        review_safety_criterion = (
            "Review safety: can the action be reviewed without sending, publishing, "
            "mutating Bloomreach, or making causal claims?"
        )
        direct_mutation_candidate = GrowthLoopDecisionCandidate(
            title="Direct Bloomreach segment or campaign mutation",
            status_label="Blocked in this slice",
            score=3,
            max_score=10,
            summary=(
                "Create or send a saved Bloomreach segment, recommendation, or campaign from "
                "the app without a separate review step."
            ),
            criteria=(
                "Schema fit: possible in concept, but the live sandbox has no saved objects to reuse yet.",
                "App evidence fit: weak because external mutation would not add app-owned paid truth by itself.",
                "Review safety: blocked because this story must not send campaigns or mutate Bloomreach.",
            ),
            outcome="Blocked because Story 130 is an explanation slice, not an execution or mutation slice.",
            boundary="No saved segment, recommendation, campaign, or external system change is created here.",
        )
    else:
        guardrail_summary = (
            "Rule-backed trace only: no live LLM call is required, no campaign is sent, "
            "no Bloomreach object is created or changed by this page load, and Loomi diagnostics "
            "do not become paid truth."
        )
        review_safety_criterion = (
            "Review safety: can the action be reviewed without sending, publishing, "
            "page-load mutation, or making causal claims?"
        )
        direct_mutation_candidate = GrowthLoopDecisionCandidate(
            title="Direct in-app Bloomreach mutation",
            status_label="Kept out of runtime",
            score=5,
            max_score=10,
            summary=(
                "Create or update Bloomreach directly from the app runtime after the "
                "recovery opportunity is selected."
            ),
            criteria=(
                "Schema fit: possible because the sleepy-goose schema supports the recovery pattern.",
                (
                    "App evidence fit: partial because the saved segment proves the mutation path, "
                    "but app-owned invoices and payments still decide revenue."
                ),
                "Review safety: held out of runtime because this page should not mutate Bloomreach on load.",
            ),
            outcome=(
                f"Held out of runtime; {bloomreach_object_proof.created_via} proves saved-segment "
                "creation instead of adding an in-app mutation button."
            ),
            boundary="No campaign or additional saved object is created by this page.",
        )

    return GrowthLoopDecisionTrace(
        title="Decision trace",
        summary=(
            "The agent compares three possible next actions and selects the recovery brief "
            "because it best matches the verified Loomi schema, this app's paid-result path, "
            "and the human-review safety boundary."
        ),
        guardrail_summary=guardrail_summary,
        scoring_criteria=(
            "Schema fit: does the action use verified cart, checkout, view, purchase, campaign, or retargeting fields?",
            "App evidence fit: does the action stay tied to tracked content, bookings, invoices, and payment-backed records?",
            review_safety_criterion,
        ),
        evidence_chain=(
            "Loomi schema proof supplies cart_update, checkout, view_item, purchase, campaign, and retargeting fields.",
            (
                "App-owned proof supplies "
                f"{_count_copy(evidence.tracked_content_count, 'content item')}, "
                f"{_count_copy(evidence.booking_count, 'booking')}, "
                f"{_count_copy(evidence.paid_invoice_count, 'paid invoice')}, and "
                f"{_money_copy(evidence.paid_revenue_cents)} canonical payment truth."
            ),
            "The selected action stays review-only so the demo can explain a next step without claiming execution or causality.",
        ),
        candidates=(
            GrowthLoopDecisionCandidate(
                title="Booking-step recovery brief",
                status_label="Selected",
                score=9,
                max_score=10,
                summary=(
                    "Prepare the reviewed recovery brief for prospects who reached a booking or "
                    "checkout-like step but have not produced a later paid result."
                ),
                criteria=(
                    "Schema fit: strong match to cart_update, checkout, view_item, purchase, campaign, and retargeting fields.",
                    "App evidence fit: strong match to the existing tracked content, booking, paid invoice, and payment truth.",
                    "Review safety: strong because the app prepares copy and a segment recipe without sending or mutating anything.",
                ),
                outcome="Selected because it is actionable, evidence-backed, and safe for human review.",
                boundary="Still review-only; success must later be measured through app-owned paid records.",
            ),
            GrowthLoopDecisionCandidate(
                title="Broad nurture follow-up",
                status_label="Held for later",
                score=6,
                max_score=10,
                summary=(
                    "Prepare a general educational follow-up for interested prospects who have not "
                    "yet shown booking-step intent."
                ),
                criteria=(
                    "Schema fit: partial because campaign engagement exists, but the cart/checkout signal is less direct.",
                    "App evidence fit: weaker because it is farther from the paid booking path already proven here.",
                    "Review safety: acceptable, but it risks diluting the demo into generic lifecycle marketing.",
                ),
                outcome="Held because it is useful later but less tightly connected to paid-result proof.",
                boundary="Would still need human review and app-owned paid-result measurement before any lift claim.",
            ),
            direct_mutation_candidate,
        ),
    )


def _build_measurement_plan(stage: str) -> GrowthLoopMeasurementPlan | None:
    if stage != GROWTH_LOOP_STAGE_PAID_RESULT_EXISTS:
        return None

    return GrowthLoopMeasurementPlan(
        title="Measurement plan",
        summary=(
            "Use this reviewer-ready plan after the segment recipe is approved and run. "
            "The app does not claim lift yet; it defines how paid outcomes would be compared."
        ),
        cards=(
            GrowthLoopMeasurementCard(
                label="Primary metric",
                title="Paid revenue",
                detail=(
                    "Measure later revenue through app-owned paid invoices and payment-backed "
                    "records in this workspace."
                ),
            ),
            GrowthLoopMeasurementCard(
                label="Supporting metric",
                title="Paid conversion rate",
                detail=(
                    "Track paid invoice count among eligible recovered prospects as supporting "
                    "conversion context."
                ),
            ),
            GrowthLoopMeasurementCard(
                label="Comparison design",
                title="Withheld holdout first",
                detail=(
                    "Compare targeted eligible prospects against eligible contacts withheld from "
                    "the recovery send; use a non-targeted comparison only when a formal holdout "
                    "was not created."
                ),
            ),
            GrowthLoopMeasurementCard(
                label="Timing",
                title="24h send, 7d observe",
                detail=(
                    "Trigger the reviewed recovery message within 24 hours of qualifying cart, "
                    "checkout, or booking-step activity, then observe app-owned paid outcomes "
                    "for 7 days."
                ),
            ),
            GrowthLoopMeasurementCard(
                label="Diagnostic context",
                title="Engagement is not revenue",
                detail=(
                    "Use campaign.status, campaign.url, retargeting.audience, and "
                    "retargeting.action to explain engagement, not to count revenue."
                ),
            ),
            GrowthLoopMeasurementCard(
                label="Claim boundary",
                title="No lift yet",
                detail=(
                    "Do not claim lift, causality, statistical confidence, or revenue "
                    "improvement until the campaign runs and app-owned paid outcomes are compared."
                ),
            ),
        ),
        limitations=(
            "This page plans measurement; it does not report measured lift or causal impact.",
            "No saved Bloomreach segment, campaign, recommendation, export, or send is created from this app.",
            "Campaign, retargeting, Loomi, and Conversation signals diagnose engagement only; canonical invoice and payment records remain paid truth.",
        ),
    )


def _build_sandbox_proof(
    stage: str,
    evidence: GrowthLoopWorkspaceEvidence,
) -> GrowthLoopSandboxProof | None:
    if stage != GROWTH_LOOP_STAGE_PAID_RESULT_EXISTS:
        return None

    tracked_content_copy = _count_copy(evidence.tracked_content_count, "content item")
    booking_copy = _count_copy(evidence.booking_count, "booking")
    paid_invoice_copy = _count_copy(evidence.paid_invoice_count, "paid invoice")
    revenue_copy = _money_copy(evidence.paid_revenue_cents)

    return GrowthLoopSandboxProof(
        title="Sandbox proof",
        summary=(
            "Story 137 connected the real sandbox surfaces to this review-only loop: "
            "Pacific Apparel Storefront shopping context, sleepy-goose Engagement event "
            "and activation surfaces, and app-owned paid proof."
        ),
        status_label="Story 137 passed",
        cards=(
            GrowthLoopSandboxProofCard(
                label="Storefront",
                title="Pacific Apparel shopping context",
                detail=(
                    "Catalog categories, cart entry point, Handbags product grid, sale and "
                    "price facets, pagination, and discount labels were visible in the "
                    "Storefront sandbox."
                ),
            ),
            GrowthLoopSandboxProofCard(
                label="Engagement",
                title="sleepy-goose activation and measurement",
                detail=(
                    "Hackathon Workspace exposes Data manager events, campaigns, analyses, "
                    "segmentations, reports, funnels, and related measurement surfaces."
                ),
            ),
            GrowthLoopSandboxProofCard(
                label="App proof",
                title="App-owned paid truth",
                detail=(
                    f"{tracked_content_copy}, {booking_copy}, {paid_invoice_copy}, "
                    f"and {revenue_copy} remain the canonical paid-result boundary."
                ),
            ),
        ),
        proof_chain=(
            "Storefront proof: Pacific Apparel supplied catalog, cart, offer, and price-filter context without checkout or Storefront mutation.",
            "Engagement proof: sleepy-goose in Hackathon Workspace exposed event, segmentation, campaign, analysis, report, and funnel surfaces for review.",
            (
                f"App proof: {tracked_content_copy}, {booking_copy}, {paid_invoice_copy}, "
                f"and {revenue_copy} canonical payment truth remain app-owned; sandbox "
                "observations do not count revenue."
            ),
        ),
        boundaries=(
            "No live Engagement or Storefront call is made by this page.",
            "No customer data, screenshots, raw event payloads, or private URLs are embedded.",
            "No campaign, report, checkout, payment, export, or Storefront mutation is performed by this page.",
            "No lift, causality, or new paid-truth source is claimed.",
        ),
    )


def _build_agent_console(
    stage: str,
    evidence: GrowthLoopWorkspaceEvidence,
    bloomreach_object_proof: GrowthLoopBloomreachObjectProof | None,
) -> GrowthLoopAgentConsole | None:
    if stage != GROWTH_LOOP_STAGE_PAID_RESULT_EXISTS:
        return None

    revenue_copy = _money_copy(evidence.paid_revenue_cents)
    paid_invoice_copy = _count_copy(evidence.paid_invoice_count, "paid invoice")
    tracked_content_copy = _count_copy(evidence.tracked_content_count, "content item")
    booking_copy = _count_copy(evidence.booking_count, "booking")
    object_steps: tuple[GrowthLoopAgentConsoleStep, ...] = ()
    object_signals: tuple[GrowthLoopCapabilitySignal, ...] = ()
    object_proof_chain: tuple[str, ...] = ()
    if bloomreach_object_proof is not None:
        object_steps = (
            GrowthLoopAgentConsoleStep(
                label="Object",
                title="Saved segment proof",
                detail=(
                    f"{bloomreach_object_proof.object_name} was created via "
                    f"{bloomreach_object_proof.created_via}; this page displays metadata only."
                ),
                status_label="Recorded",
            ),
        )
        object_signals = (
            GrowthLoopCapabilitySignal(
                label="Bloomreach saved segment",
                value=bloomreach_object_proof.object_name,
                detail=(
                    f"{bloomreach_object_proof.object_type} {bloomreach_object_proof.object_id} "
                    f"was recorded from {bloomreach_object_proof.created_via}."
                ),
            ),
        )
        object_proof_chain = (
            (
                f"Bloomreach object proof supplies saved segment "
                f"{bloomreach_object_proof.object_name} ({bloomreach_object_proof.object_id})."
            ),
        )

    segment_evidence_label = "Bloomreach-ready recipe"
    segment_evidence_detail = (
        "The app prepares a recipe only; it does not create a saved segment, "
        "recommendation, or campaign."
    )
    completion_summary = (
        "The agent has prepared one review-only packet. It is ready for human review, "
        "not for automatic send, export, or mutation."
    )
    guided_boundaries = (
        "No campaign is sent.",
        "No Bloomreach object is mutated.",
        "No lift or causality is claimed.",
        "App-owned invoice and payment records remain paid truth.",
    )
    packet_boundaries = (
        "No campaign is sent.",
        "No Bloomreach object is mutated.",
        "No lift is claimed yet.",
        "App-owned invoice and payment records remain paid truth.",
    )
    if bloomreach_object_proof is not None:
        segment_evidence_label = "Bloomreach saved segment proof"
        segment_evidence_detail = (
            f"Recorded segment {bloomreach_object_proof.object_name} proves the mutation path; "
            "this page does not create or update it on load."
        )
        completion_summary = (
            "The agent has prepared one review packet with recorded saved-segment proof. "
            "It is ready for human review, not for automatic send, export, or page-load mutation."
        )
        guided_boundaries = (
            "No campaign is sent.",
            "No Bloomreach object is created or changed by this page load.",
            "Recorded saved segment proof remains review-only.",
            "No lift or causality is claimed.",
            "App-owned invoice and payment records remain paid truth.",
        )
        packet_boundaries = (
            "No campaign is sent.",
            "No Bloomreach object is created or changed by this page load.",
            "Recorded saved segment proof remains review-only.",
            "No lift is claimed yet.",
            "App-owned invoice and payment records remain paid truth.",
        )

    return GrowthLoopAgentConsole(
        title="Agent console",
        summary=(
            "Paid result exists; the agent packaged the proof, schema opportunity, "
            "reviewable action, segment recipe, measurement plan, and available saved-segment "
            "proof into one review packet."
        ),
        primary_action_label="View review packet",
        steps=(
            GrowthLoopAgentConsoleStep(
                label="Proof",
                title="Paid result boundary",
                detail=(
                    f"{paid_invoice_copy} and {revenue_copy} are counted only from app-owned "
                    "invoice and payment records."
                ),
                status_label="Verified",
            ),
            GrowthLoopAgentConsoleStep(
                label="Schema",
                title="Bloomreach opportunity",
                detail=(
                    "Cursor MCP proof showed cart_update, checkout, view_item, purchase, "
                    "campaign, and retargeting fields in sleepy-goose."
                ),
                status_label="Mapped",
            ),
            *object_steps,
            GrowthLoopAgentConsoleStep(
                label="Action",
                title="Reviewable recovery brief",
                detail="Prepare one booking-step recovery brief for human review before any send.",
                status_label="Selected",
            ),
            GrowthLoopAgentConsoleStep(
                label="Segment",
                title="Bloomreach-ready recipe",
                detail="Use include/exclude rules and a 24-hour recovery window a marketer can recreate.",
                status_label="Ready",
            ),
            GrowthLoopAgentConsoleStep(
                label="Measure",
                title="Holdout-first plan",
                detail="Measure paid revenue and paid conversion rate after execution; no lift is claimed yet.",
                status_label="Defined",
            ),
        ),
        guided_run=GrowthLoopGuidedRun(
            title="Run agent",
            summary=(
                "Step through the controlled workflow the demo uses: inspect paid proof, "
                "read Loomi schema evidence, score actions, and assemble the review packet."
            ),
            primary_action_label="Run next step",
            completion_title="Review packet assembled",
            completion_summary=completion_summary,
            steps=(
                GrowthLoopGuidedRunStep(
                    label="1",
                    title="Inspect paid proof",
                    summary=(
                        "Read the app-owned tracked content, booking, invoice, and payment records "
                        "that define the paid-result boundary."
                    ),
                    evidence_label="App-owned proof",
                    evidence_detail=(
                        f"{tracked_content_copy}, {booking_copy}, {paid_invoice_copy}, and "
                        f"{revenue_copy} canonical payment truth."
                    ),
                    target_anchor="#growth-loop-boundaries",
                ),
                GrowthLoopGuidedRunStep(
                    label="2",
                    title="Read Loomi schema evidence",
                    summary=(
                        "Use the Cursor-verified sleepy-goose schema as the diagnostic signal "
                        "for a cart-abandon recovery opportunity."
                    ),
                    evidence_label="MCP-derived schema",
                    evidence_detail=(
                        "cart_update, checkout, view_item, purchase, campaign, and retargeting "
                        "fields support the recovery blueprint."
                    ),
                    target_anchor="#growth-loop-proof",
                ),
                GrowthLoopGuidedRunStep(
                    label="3",
                    title="Score candidate actions",
                    summary=(
                        "Compare recovery, broad nurture, and direct mutation candidates using "
                        "schema fit, app evidence fit, and review safety."
                    ),
                    evidence_label="Decision trace",
                    evidence_detail=(
                        "Recovery wins because it is actionable, evidence-backed, and safe for "
                        "human review."
                    ),
                    target_anchor="#growth-loop-decision",
                ),
                GrowthLoopGuidedRunStep(
                    label="4",
                    title="Prepare recovery brief",
                    summary=(
                        "Turn the selected opportunity into a copy-ready brief a marketer can "
                        "review before any customer-facing action."
                    ),
                    evidence_label="Prepared artifact",
                    evidence_detail=(
                        "The brief includes target segment, message outline, success evidence, "
                        "and no-send/no-mutation boundaries."
                    ),
                    target_anchor="#growth-loop-action",
                ),
                GrowthLoopGuidedRunStep(
                    label="5",
                    title="Generate segment recipe",
                    summary=(
                        "Translate the brief into include, exclude, timing, and message-variable "
                        "logic that can be manually recreated in Bloomreach."
                    ),
                    evidence_label=segment_evidence_label,
                    evidence_detail=segment_evidence_detail,
                    target_anchor="#growth-loop-segment",
                ),
                GrowthLoopGuidedRunStep(
                    label="6",
                    title="Attach measurement plan",
                    summary=(
                        "Define how future success would be evaluated through app-owned paid "
                        "outcomes and a withheld holdout."
                    ),
                    evidence_label="No-lift-yet plan",
                    evidence_detail=(
                        "Paid revenue is primary, paid conversion is supporting, and engagement "
                        "signals remain diagnostic only."
                    ),
                    target_anchor="#growth-loop-measure",
                ),
            ),
            boundaries=guided_boundaries,
        ),
        capability_signals=(
            GrowthLoopCapabilitySignal(
                label="App-owned paid truth",
                value=f"{paid_invoice_copy}, {revenue_copy}",
                detail="Canonical invoice and payment records decide revenue.",
            ),
            *object_signals,
            GrowthLoopCapabilitySignal(
                label="Cursor MCP schema proof",
                value="sleepy-goose",
                detail="Verified before runtime; not a live page-load MCP call.",
            ),
            GrowthLoopCapabilitySignal(
                label="PayPal-shaped outcome proof",
                value="Order/capture seed",
                detail="Demonstrates paid-result flow without a live PayPal payment.",
            ),
            GrowthLoopCapabilitySignal(
                label="Review-only action",
                value="No send",
                detail="The app prepares review artifacts and does not mutate external systems.",
            ),
        ),
        review_packet=GrowthLoopReviewPacket(
            title="Review packet",
            summary=(
                "One compact packet for judges and reviewers: what was proven, which recovery "
                "loop was selected, how Bloomreach can recreate it, and how success would be measured."
            ),
            selected_action=(
                "Booking-step recovery brief for prospects who reached a booking or checkout-like "
                "step but have no later paid result."
            ),
            segment_summary=(
                "Bloomreach-ready recipe: include non-empty cart_update or booking-step analogues, "
                "exclude later completed purchases or app-owned paid invoices, and default to a "
                "24-hour recovery window."
            ),
            measurement_summary=(
                "Primary metric is paid revenue; supporting metric is paid conversion rate. "
                "Use a withheld holdout first and claim no lift until the campaign runs and "
                "app-owned paid outcomes are compared."
            ),
            proof_chain=(
                "Loomi schema proof supplies cart_update, checkout, view_item, purchase, campaign, and retargeting fields.",
                *object_proof_chain,
                (
                    f"App-owned proof supplies {tracked_content_copy}, "
                    f"{booking_copy}, {paid_invoice_copy}, and "
                    f"{revenue_copy} canonical payment truth."
                ),
                "The selected action stays review-only so the app can explain a next step without claiming execution or causality.",
            ),
            boundaries=packet_boundaries,
        ),
    )


def _build_reviewable_action_brief(
    stage: str,
    bloomreach_object_proof: GrowthLoopBloomreachObjectProof | None,
) -> GrowthLoopReviewableActionBrief | None:
    if stage != GROWTH_LOOP_STAGE_PAID_RESULT_EXISTS:
        return None

    if bloomreach_object_proof is None:
        bloomreach_next_step = (
            "Draft a segment spec using cart_update, checkout, view_item, and purchase exclusion logic.",
            "Keep the segment as a human-reviewed recipe until someone recreates it inside Bloomreach.",
            "Use campaign and retargeting events only as diagnostic engagement signals after review.",
        )
        limitations = (
            "Prepared for human review only; this app does not send the recovery message.",
            "This draft does not mutate Bloomreach or create a saved segment, campaign, or recommendation.",
            "It does not count revenue, prove causality, or replace app-owned booking, invoice, and payment records.",
        )
    else:
        bloomreach_next_step = (
            "Review the recorded saved segment against cart_update, checkout, view_item, and purchase exclusion logic.",
            "Use the saved segment proof as evidence that the Bloomreach mutation path works, but keep activation behind human review.",
            "Use campaign and retargeting events only as diagnostic engagement signals after review.",
        )
        limitations = (
            "Prepared for human review only; this app does not send the recovery message.",
            f"The saved segment proof was created through {bloomreach_object_proof.created_via}; this page does not mutate Bloomreach on load.",
            "It does not count revenue, prove causality, or replace app-owned booking, invoice, and payment records.",
        )

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
        bloomreach_next_step=bloomreach_next_step,
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
        limitations=limitations,
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
