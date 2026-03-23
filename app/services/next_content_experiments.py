import hashlib
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.content import Content
from app.models.creator_experiment_run import CreatorExperimentRunRecord
from app.models.creator_experiment_run_card import CreatorExperimentRunCardRecord
from app.services.authoritative_content_evidence import (
    AuthoritativeContentEvidence,
    get_authoritative_content_evidence,
)
from app.services.booking_attribution import (
    BOOKING_ATTRIBUTION_STATUS_UNATTRIBUTED,
    list_creator_booking_attribution_rows,
)
from app.services.creator_claim_snapshots import (
    CreateCreatorClaimSnapshotInput,
    create_creator_claim_snapshot,
    resolve_creator_claim_snapshot,
)
from app.services.settled_paid_evidence import (
    CreatorSettledPaidEvidenceSnapshot,
    SettledPaidEvidenceRow,
    get_creator_settled_paid_evidence,
)

EXPERIMENT_RUN_STATUS_READY = "ready"
EXPERIMENT_RUN_STATUS_UNSUPPORTED = "unsupported"
EXPERIMENT_GENERATOR_TYPE = "deterministic_rules"
EXPERIMENT_RUN_CONTRACT_VERSION = "next_content_experiments_helper.v1"
EXPERIMENT_RUN_REDUCER_VERSION = "next_content_experiments.rules.v1"
EXPERIMENT_RUN_CONFIG_VERSION = "next_content_experiments.helper_config.v2"
EXPERIMENT_RESULT_SCHEMA_VERSION = "next_content_experiments.result.v2"
EXPERIMENT_EVIDENCE_INPUT_VERSION = "next_content_experiments.snapshot_inputs.v1"
EXPERIMENT_FRESHNESS_POLICY_VERSION = "next_content_experiments.freshness_policy.v1"
EXPERIMENT_CARD_ID_VERSION = "next_content_experiment_card_id.v1"
EXPERIMENT_AUTHORITATIVE_CONTENT_WINDOW = (
    "No max age cutoff. Use the current authoritative content snapshot at generation time."
)
EXPERIMENT_SETTLED_PAID_WINDOW = (
    "No max age cutoff. Use all settled attributed paid results available at generation time."
)
EXPERIMENT_RERUN_BEHAVIOR = (
    "Snapshots are immutable after generation. Refreshing the page does not rerun the helper."
)
EXPERIMENT_CARD_CLAIM_KIND = "next_content_experiment_card"
EXPERIMENT_CARD_CLAIM_CONTRACT_VERSION = "next_content_experiment_card.v1"
EXPERIMENT_CARD_CLAIM_REDUCER_VERSION = EXPERIMENT_RUN_REDUCER_VERSION
EXPERIMENT_CARD_CLAIM_CONFIG_VERSION = "next_content_experiment_card.rendering_config.v2"
MAX_EXPERIMENT_CARDS = 3
UNSUPPORTED_EXPERIMENTS_SUMMARY = (
    "Not enough trusted evidence yet to suggest next content experiments. "
    "Finish reviewing content topics or wait for more attributed paid results."
)

ExperimentRunStatus = Literal["ready", "unsupported"]


@dataclass(frozen=True)
class HelperGenerationLineage:
    generator_type: str | None
    model_name: str | None
    prompt_version: str | None
    config_version: str | None
    contract_version: str
    reducer_version: str | None


@dataclass(frozen=True)
class NextContentExperimentsGenerationSpec:
    run_lineage: HelperGenerationLineage
    card_lineage: HelperGenerationLineage


@dataclass(frozen=True)
class HelperVersionSemantics:
    schema_version: str
    evidence_input_version: str
    generation_config_version: str | None


@dataclass(frozen=True)
class HelperFreshnessPolicy:
    policy_version: str
    authoritative_content_window: str
    settled_paid_window: str
    rerun_behavior: str


CURRENT_NEXT_CONTENT_EXPERIMENTS_GENERATION_SPEC = NextContentExperimentsGenerationSpec(
    run_lineage=HelperGenerationLineage(
        generator_type=EXPERIMENT_GENERATOR_TYPE,
        model_name=None,
        prompt_version=None,
        config_version=EXPERIMENT_RUN_CONFIG_VERSION,
        contract_version=EXPERIMENT_RUN_CONTRACT_VERSION,
        reducer_version=EXPERIMENT_RUN_REDUCER_VERSION,
    ),
    card_lineage=HelperGenerationLineage(
        generator_type=EXPERIMENT_GENERATOR_TYPE,
        model_name=None,
        prompt_version=None,
        config_version=EXPERIMENT_CARD_CLAIM_CONFIG_VERSION,
        contract_version=EXPERIMENT_CARD_CLAIM_CONTRACT_VERSION,
        reducer_version=EXPERIMENT_CARD_CLAIM_REDUCER_VERSION,
    ),
)

CURRENT_NEXT_CONTENT_EXPERIMENTS_FRESHNESS_POLICY = HelperFreshnessPolicy(
    policy_version=EXPERIMENT_FRESHNESS_POLICY_VERSION,
    authoritative_content_window=EXPERIMENT_AUTHORITATIVE_CONTENT_WINDOW,
    settled_paid_window=EXPERIMENT_SETTLED_PAID_WINDOW,
    rerun_behavior=EXPERIMENT_RERUN_BEHAVIOR,
)


@dataclass(frozen=True)
class NextContentExperimentCard:
    title: str
    hypothesis: str
    why_this_might_work: str
    evidence_summary: str
    content_tids: list[str]
    caution: str
    ranking_rationale: str | None = None
    card_id: str | None = None
    card_claim_snapshot_id: UUID | None = None
    card_order: int | None = None
    lineage: HelperGenerationLineage | None = None


@dataclass(frozen=True)
class CreatorNextContentExperimentsResult:
    claim_snapshot_id: UUID
    status: ExperimentRunStatus
    summary: str
    lineage: HelperGenerationLineage
    version_semantics: HelperVersionSemantics
    freshness_policy: HelperFreshnessPolicy
    experiments: list[NextContentExperimentCard]
    created_at: datetime


@dataclass(frozen=True)
class NextContentExperimentUnsupportedExplanation:
    reasons: list[str]
    has_excluded_current_activity: bool


@dataclass(frozen=True)
class NextContentExperimentPaidEvidenceDetail:
    content_tid: str
    booked_at: datetime
    paid_at: datetime
    amount_cents: int
    currency: str


@dataclass(frozen=True)
class CreatorNextContentExperimentCardDrilldown:
    run_claim_snapshot_id: UUID
    card_claim_snapshot_id: UUID
    card_id: str | None
    created_at: datetime
    run_lineage: HelperGenerationLineage
    card_lineage: HelperGenerationLineage
    version_semantics: HelperVersionSemantics
    freshness_policy: HelperFreshnessPolicy
    card_order: int
    title: str
    hypothesis: str
    why_this_might_work: str
    ranking_rationale: str | None
    caution: str
    authoritative_source_url: str
    authoritative_content_tid: str
    authoritative_artifact_title: str | None
    authoritative_topics: list[str]
    settled_paid_results: list[NextContentExperimentPaidEvidenceDetail]


@dataclass(frozen=True)
class _ExperimentCandidate:
    content: Content
    authoritative_evidence: AuthoritativeContentEvidence
    settled_paid_rows: list[SettledPaidEvidenceRow]
    primary_topic_label: str
    paid_booking_count: int
    paid_invoice_count: int
    paid_revenue_cents: int
    last_paid_at: datetime


@dataclass(frozen=True)
class CreatorNextContentExperimentCardComparison:
    stable_card_id: str | None
    baseline_card: NextContentExperimentCard | None
    candidate_card: NextContentExperimentCard | None


@dataclass(frozen=True)
class CreatorNextContentExperimentsRunComparison:
    baseline_run: CreatorNextContentExperimentsResult
    candidate_run: CreatorNextContentExperimentsResult
    card_comparisons: list[CreatorNextContentExperimentCardComparison]


def create_creator_next_content_experiments_run(
    *,
    creator_id: UUID,
    db: Session,
    generation_spec: NextContentExperimentsGenerationSpec = CURRENT_NEXT_CONTENT_EXPERIMENTS_GENERATION_SPEC,
) -> CreatorNextContentExperimentsResult:
    settled_snapshot = get_creator_settled_paid_evidence(
        creator_id=creator_id,
        db=db,
    )
    candidates = _build_experiment_candidates(
        creator_id=creator_id,
        settled_paid_rows=settled_snapshot.settled_rows,
        db=db,
    )

    run_record = CreatorExperimentRunRecord(
        creator_id=creator_id,
        status=EXPERIMENT_RUN_STATUS_UNSUPPORTED,
        summary_text=UNSUPPORTED_EXPERIMENTS_SUMMARY,
        run_generator_type=generation_spec.run_lineage.generator_type,
        run_model_name=generation_spec.run_lineage.model_name,
        run_config_version=generation_spec.run_lineage.config_version,
        run_contract_version=generation_spec.run_lineage.contract_version,
        run_reducer_version=generation_spec.run_lineage.reducer_version,
        run_prompt_version=generation_spec.run_lineage.prompt_version,
    )
    db.add(run_record)
    db.flush()

    if candidates:
        selected_candidates = sorted(candidates, key=_candidate_sort_key)[:MAX_EXPERIMENT_CARDS]
        cards: list[NextContentExperimentCard] = []
        run_record.status = EXPERIMENT_RUN_STATUS_READY
        run_record.summary_text = _ready_summary(card_count=len(selected_candidates))

        for index, candidate in enumerate(selected_candidates, start=1):
            card = _build_experiment_card(
                candidate=candidate,
                selected_candidates=selected_candidates,
                rank=index,
            )
            claim_snapshot = create_creator_claim_snapshot(
                creator_id=creator_id,
                input=CreateCreatorClaimSnapshotInput(
                    claim_kind=EXPERIMENT_CARD_CLAIM_KIND,
                    content_id=candidate.content.id,
                    authoritative_extraction_artifact_id=candidate.authoritative_evidence.artifact.id,
                    authoritative_fetch_snapshot_id=candidate.authoritative_evidence.fetch_snapshot.id,
                    settled_paid_evidence_rows=candidate.settled_paid_rows,
                    claim_generator_type=generation_spec.card_lineage.generator_type,
                    claim_model_name=generation_spec.card_lineage.model_name,
                    claim_config_version=generation_spec.card_lineage.config_version,
                    claim_contract_version=generation_spec.card_lineage.contract_version,
                    claim_reducer_version=generation_spec.card_lineage.reducer_version,
                    claim_prompt_version=generation_spec.card_lineage.prompt_version,
                    rendered_claim_text=_rendered_claim_text(card=card),
                ),
                db=db,
            )
            run_record.cards.append(
                CreatorExperimentRunCardRecord(
                    claim_snapshot_id=claim_snapshot.id,
                    card_id=card.card_id,
                    content_tid=card.content_tids[0],
                    title=card.title,
                    hypothesis=card.hypothesis,
                    why_this_might_work=card.why_this_might_work,
                    evidence_summary=card.evidence_summary,
                    ranking_rationale=card.ranking_rationale,
                    caution=card.caution,
                    card_order=index,
                )
            )
            cards.append(card)

        _validate_experiment_result(
            status=run_record.status,
            summary=run_record.summary_text,
            experiments=cards,
        )
    else:
        _validate_experiment_result(
            status=run_record.status,
            summary=run_record.summary_text,
            experiments=[],
        )

    db.flush()
    db.refresh(run_record)
    hydrated_run_record = db.execute(
        _creator_experiment_run_query(creator_id=creator_id).where(
            CreatorExperimentRunRecord.id == run_record.id
        )
    ).scalar_one()
    return _build_experiment_result(hydrated_run_record)


def get_latest_creator_next_content_experiments_run(
    *,
    creator_id: UUID,
    db: Session,
) -> CreatorNextContentExperimentsResult | None:
    run_record = db.execute(
        _creator_experiment_run_query(creator_id=creator_id)
        .order_by(
            CreatorExperimentRunRecord.created_at.desc(),
            CreatorExperimentRunRecord.id.desc(),
        )
    ).scalars().first()
    if run_record is None:
        return None
    return _build_experiment_result(run_record)


def get_creator_next_content_experiments_run(
    *,
    creator_id: UUID,
    claim_snapshot_id: UUID,
    db: Session,
) -> CreatorNextContentExperimentsResult | None:
    run_record = db.execute(
        _creator_experiment_run_query(creator_id=creator_id).where(
            CreatorExperimentRunRecord.id == claim_snapshot_id
        )
    ).scalar_one_or_none()
    if run_record is None:
        return None
    return _build_experiment_result(run_record)


def get_creator_next_content_experiment_card_drilldown(
    *,
    creator_id: UUID,
    run_claim_snapshot_id: UUID,
    card_order: int,
    db: Session,
) -> CreatorNextContentExperimentCardDrilldown | None:
    run_record = db.execute(
        _creator_experiment_run_query(creator_id=creator_id).where(
            CreatorExperimentRunRecord.id == run_claim_snapshot_id
        )
    ).scalar_one_or_none()
    if run_record is None:
        return None

    card_record = next(
        (card for card in run_record.cards if card.card_order == card_order),
        None,
    )
    if card_record is None:
        return None
    return _build_experiment_card_drilldown(
        creator_id=creator_id,
        run_record=run_record,
        card_record=card_record,
        db=db,
    )


def get_creator_next_content_experiment_card_drilldown_by_card_id(
    *,
    creator_id: UUID,
    run_claim_snapshot_id: UUID,
    card_id: str,
    db: Session,
) -> CreatorNextContentExperimentCardDrilldown | None:
    run_record = db.execute(
        _creator_experiment_run_query(creator_id=creator_id).where(
            CreatorExperimentRunRecord.id == run_claim_snapshot_id
        )
    ).scalar_one_or_none()
    if run_record is None:
        return None

    card_record = next(
        (card for card in run_record.cards if card.card_id == card_id),
        None,
    )
    if card_record is None:
        return None

    return _build_experiment_card_drilldown(
        creator_id=creator_id,
        run_record=run_record,
        card_record=card_record,
        db=db,
    )


def _build_experiment_card_drilldown(
    *,
    creator_id: UUID,
    run_record: CreatorExperimentRunRecord,
    card_record: CreatorExperimentRunCardRecord,
    db: Session,
) -> CreatorNextContentExperimentCardDrilldown | None:

    resolved_snapshot = resolve_creator_claim_snapshot(
        creator_id=creator_id,
        claim_snapshot_id=card_record.claim_snapshot_id,
        db=db,
    )
    if resolved_snapshot is None:
        return None

    authoritative_content = resolved_snapshot.authoritative_content_evidence
    content = authoritative_content.artifact.content
    run_lineage = _build_run_lineage(run_record)
    return CreatorNextContentExperimentCardDrilldown(
        run_claim_snapshot_id=run_record.id,
        card_claim_snapshot_id=card_record.claim_snapshot_id,
        card_id=card_record.card_id,
        created_at=run_record.created_at,
        run_lineage=run_lineage,
        card_lineage=_build_claim_lineage(resolved_snapshot.snapshot),
        version_semantics=_build_version_semantics(run_lineage),
        freshness_policy=CURRENT_NEXT_CONTENT_EXPERIMENTS_FRESHNESS_POLICY,
        card_order=card_record.card_order,
        title=card_record.title,
        hypothesis=card_record.hypothesis,
        why_this_might_work=card_record.why_this_might_work,
        ranking_rationale=card_record.ranking_rationale,
        caution=card_record.caution,
        authoritative_source_url=content.source_url,
        authoritative_content_tid=content.tid,
        authoritative_artifact_title=authoritative_content.artifact.title,
        authoritative_topics=[
            topic.canonical_label for topic in authoritative_content.confirmed_topics
        ],
        settled_paid_results=[
            NextContentExperimentPaidEvidenceDetail(
                content_tid=row.tid,
                booked_at=row.booked_at,
                paid_at=row.invoice_paid_at,
                amount_cents=row.invoice_amount_cents,
                currency=row.invoice_currency,
            )
            for row in resolved_snapshot.settled_paid_evidence_rows
        ],
    )


def compare_creator_next_content_experiments_runs(
    *,
    creator_id: UUID,
    baseline_claim_snapshot_id: UUID,
    candidate_claim_snapshot_id: UUID,
    db: Session,
) -> CreatorNextContentExperimentsRunComparison | None:
    baseline_run = get_creator_next_content_experiments_run(
        creator_id=creator_id,
        claim_snapshot_id=baseline_claim_snapshot_id,
        db=db,
    )
    if baseline_run is None:
        return None

    candidate_run = get_creator_next_content_experiments_run(
        creator_id=creator_id,
        claim_snapshot_id=candidate_claim_snapshot_id,
        db=db,
    )
    if candidate_run is None:
        return None

    baseline_cards_by_key = {
        _card_comparison_key(card): card for card in baseline_run.experiments
    }
    candidate_cards_by_key = {
        _card_comparison_key(card): card for card in candidate_run.experiments
    }
    ordered_keys: list[tuple[str, str]] = []
    seen_keys: set[tuple[str, str]] = set()
    for card in baseline_run.experiments + candidate_run.experiments:
        key = _card_comparison_key(card)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        ordered_keys.append(key)

    return CreatorNextContentExperimentsRunComparison(
        baseline_run=baseline_run,
        candidate_run=candidate_run,
        card_comparisons=[
            CreatorNextContentExperimentCardComparison(
                stable_card_id=_comparison_card_id(
                    baseline_cards_by_key.get(key),
                    candidate_cards_by_key.get(key),
                ),
                baseline_card=baseline_cards_by_key.get(key),
                candidate_card=candidate_cards_by_key.get(key),
            )
            for key in ordered_keys
        ],
    )


def get_current_creator_next_content_experiments_unsupported_explanation(
    *,
    creator_id: UUID,
    db: Session,
) -> NextContentExperimentUnsupportedExplanation:
    settled_snapshot = get_creator_settled_paid_evidence(
        creator_id=creator_id,
        db=db,
    )
    authoritative_content_ids = _load_authoritative_content_ids_with_topics(
        creator_id=creator_id,
        db=db,
    )
    settled_content_ids = {row.content_id for row in settled_snapshot.settled_rows}

    reasons: list[str] = []
    if not authoritative_content_ids:
        reasons.append("No authoritative reviewed topics exist yet on your tracked content.")
    if not settled_content_ids:
        reasons.append("No settled attributed paid results exist yet for this workspace.")
    if authoritative_content_ids and settled_content_ids and not (
        authoritative_content_ids & settled_content_ids
    ):
        reasons.append(
            "Your reviewed topics and settled paid results do not overlap on the same tracked content yet."
        )

    return NextContentExperimentUnsupportedExplanation(
        reasons=reasons,
        has_excluded_current_activity=_has_excluded_current_activity(
            creator_id=creator_id,
            settled_snapshot=settled_snapshot,
            db=db,
        ),
    )


def _creator_experiment_run_query(*, creator_id: UUID):
    return (
        select(CreatorExperimentRunRecord)
        .options(
            selectinload(CreatorExperimentRunRecord.cards).selectinload(
                CreatorExperimentRunCardRecord.claim_snapshot
            )
        )
        .where(CreatorExperimentRunRecord.creator_id == creator_id)
    )


def _build_experiment_candidates(
    *,
    creator_id: UUID,
    settled_paid_rows: list[SettledPaidEvidenceRow],
    db: Session,
) -> list[_ExperimentCandidate]:
    if not settled_paid_rows:
        return []

    content_ids = sorted({row.content_id for row in settled_paid_rows}, key=str)
    content_rows = db.execute(
        select(Content).where(
            Content.creator_id == creator_id,
            Content.id.in_(content_ids),
        )
    ).scalars().all()
    content_by_id = {content.id: content for content in content_rows}
    settled_rows_by_content_id: dict[UUID, list[SettledPaidEvidenceRow]] = defaultdict(list)
    for row in settled_paid_rows:
        settled_rows_by_content_id[row.content_id].append(row)

    candidates: list[_ExperimentCandidate] = []
    for content_id, rows in settled_rows_by_content_id.items():
        content = content_by_id.get(content_id)
        if content is None:
            continue

        authoritative_evidence = get_authoritative_content_evidence(
            content=content,
            db=db,
        )
        if authoritative_evidence is None or not authoritative_evidence.confirmed_topics:
            continue

        candidates.append(
            _ExperimentCandidate(
                content=content,
                authoritative_evidence=authoritative_evidence,
                settled_paid_rows=sorted(
                    rows,
                    key=lambda row: (
                        row.invoice_paid_at,
                        str(row.booking_id),
                    ),
                    reverse=True,
                ),
                primary_topic_label=authoritative_evidence.confirmed_topics[0].canonical_label,
                paid_booking_count=len({row.booking_id for row in rows}),
                paid_invoice_count=len({row.invoice_id for row in rows}),
                paid_revenue_cents=sum(row.invoice_amount_cents for row in rows),
                last_paid_at=max(row.invoice_paid_at for row in rows),
            )
        )

    return candidates


def _load_authoritative_content_ids_with_topics(
    *,
    creator_id: UUID,
    db: Session,
) -> set[UUID]:
    content_rows = db.execute(
        select(Content).where(Content.creator_id == creator_id)
    ).scalars().all()

    authoritative_content_ids: set[UUID] = set()
    for content in content_rows:
        authoritative_evidence = get_authoritative_content_evidence(
            content=content,
            db=db,
        )
        if authoritative_evidence is None or not authoritative_evidence.confirmed_topics:
            continue
        authoritative_content_ids.add(content.id)

    return authoritative_content_ids


def _has_excluded_current_activity(
    *,
    creator_id: UUID,
    settled_snapshot: CreatorSettledPaidEvidenceSnapshot,
    db: Session,
) -> bool:
    if settled_snapshot.unmatched_payment_backlog.event_count > 0:
        return True
    if settled_snapshot.blocked_billing_backlog.open_case_count > 0:
        return True

    booking_rows = list_creator_booking_attribution_rows(
        creator_id=creator_id,
        db=db,
    )
    return any(
        row.attribution.status == BOOKING_ATTRIBUTION_STATUS_UNATTRIBUTED
        for row in booking_rows
    )


def _candidate_sort_key(candidate: _ExperimentCandidate) -> tuple[int, int, int, str]:
    return (
        -candidate.paid_booking_count,
        -candidate.paid_revenue_cents,
        -int(candidate.last_paid_at.timestamp()),
        candidate.content.tid,
    )


def _build_experiment_result(
    run_record: CreatorExperimentRunRecord,
) -> CreatorNextContentExperimentsResult:
    experiments = [
        NextContentExperimentCard(
            card_id=card.card_id,
            card_claim_snapshot_id=card.claim_snapshot_id,
            card_order=card.card_order,
            lineage=_build_card_record_lineage(card),
            title=card.title,
            hypothesis=card.hypothesis,
            why_this_might_work=card.why_this_might_work,
            evidence_summary=card.evidence_summary,
            content_tids=[card.content_tid],
            caution=card.caution,
            ranking_rationale=card.ranking_rationale,
        )
        for card in run_record.cards
    ]
    _validate_experiment_result(
        status=run_record.status,
        summary=run_record.summary_text,
        experiments=experiments,
    )
    run_lineage = _build_run_lineage(run_record)
    return CreatorNextContentExperimentsResult(
        claim_snapshot_id=run_record.id,
        status=run_record.status,
        summary=run_record.summary_text,
        lineage=run_lineage,
        version_semantics=_build_version_semantics(run_lineage),
        freshness_policy=CURRENT_NEXT_CONTENT_EXPERIMENTS_FRESHNESS_POLICY,
        experiments=experiments,
        created_at=run_record.created_at,
    )


def _build_experiment_card(
    *,
    candidate: _ExperimentCandidate,
    selected_candidates: list[_ExperimentCandidate],
    rank: int,
) -> NextContentExperimentCard:
    revenue_summary = _format_revenue_summary(candidate.settled_paid_rows)
    topic_label = candidate.primary_topic_label
    tid = candidate.content.tid
    source_url = candidate.content.source_url
    title = _truncate_title(f"Test another {topic_label} angle")
    hypothesis = (
        f"Test whether another post about {topic_label} may lead to more attributed paid bookings."
    )
    why_this_might_work = (
        f"Your authoritative content at {source_url} already links the topic "
        f'"{topic_label}" to {candidate.paid_booking_count} paid '
        f'booking{"s" if candidate.paid_booking_count != 1 else ""}.'
    )
    evidence_summary = (
        f'Authoritative content pattern: "{topic_label}" on tracking ID {tid}. '
        f"Settled paid pattern: {candidate.paid_booking_count} paid "
        f'booking{"s" if candidate.paid_booking_count != 1 else ""} across '
        f"{candidate.paid_invoice_count} paid "
        f'invoice{"s" if candidate.paid_invoice_count != 1 else ""} totaling {revenue_summary}.'
    )
    ranking_rationale = build_next_content_experiment_ranking_rationale(
        rank=rank,
        paid_booking_count=candidate.paid_booking_count,
        paid_invoice_count=candidate.paid_invoice_count,
        revenue_summary=revenue_summary,
        reason=_experiment_card_ranking_reason(
            selected_candidates=selected_candidates,
            rank=rank,
        ),
    )
    caution = (
        "Treat this as a hypothesis, not a guarantee. This card is grounded in one "
        "authoritative content pattern and this creator's settled paid results for one tracked post."
    )
    return NextContentExperimentCard(
        title=title,
        hypothesis=hypothesis,
        why_this_might_work=why_this_might_work,
        evidence_summary=evidence_summary,
        content_tids=[tid],
        caution=caution,
        ranking_rationale=ranking_rationale,
        card_id=_build_experiment_card_id(candidate=candidate),
    )


def build_next_content_experiment_ranking_rationale(
    *,
    rank: int,
    paid_booking_count: int,
    paid_invoice_count: int,
    revenue_summary: str,
    reason: Literal[
        "only_supported_pattern",
        "paid_bookings",
        "paid_revenue",
        "recency",
        "deterministic_tie_breaker",
    ],
) -> str:
    booking_copy = (
        f"{paid_booking_count} paid booking"
        f'{"s" if paid_booking_count != 1 else ""}'
    )
    invoice_copy = (
        f"{paid_invoice_count} paid invoice"
        f'{"s" if paid_invoice_count != 1 else ""}'
    )
    evidence_copy = f"{booking_copy} across {invoice_copy} totaling {revenue_summary}"

    if rank == 1:
        if reason == "only_supported_pattern":
            return (
                "It is the only supported pattern in your current snapshot, with "
                f"{evidence_copy}."
            )
        if reason == "paid_bookings":
            return f"It leads your current snapshot on paid bookings, with {evidence_copy}."
        if reason == "paid_revenue":
            return (
                "It is tied on paid bookings but leads on attributed revenue, with "
                f"{evidence_copy}."
            )
        if reason == "recency":
            return (
                "It is tied on paid bookings and attributed revenue but is the most recent "
                f"supported paid pattern, with {evidence_copy}."
            )
        return (
            "It remains first after the current deterministic tie-breakers, with "
            f"{evidence_copy}."
        )

    if reason == "paid_bookings":
        return (
            f"It is still supported by {evidence_copy}, but it ranks below the card above "
            "because that pattern has more paid bookings."
        )
    if reason == "paid_revenue":
        return (
            f"It is still supported by {evidence_copy}, but it ranks below the card above "
            "because that pattern is tied on paid bookings and leads on attributed revenue."
        )
    if reason == "recency":
        return (
            f"It is still supported by {evidence_copy}, but it ranks below the card above "
            "because that pattern is tied on paid bookings and attributed revenue and is more recent."
        )
    return (
        f"It is still supported by {evidence_copy}, but it ranks below the card above after "
        "the current deterministic tie-breakers."
    )


def _experiment_card_ranking_reason(
    *,
    selected_candidates: list[_ExperimentCandidate],
    rank: int,
) -> Literal[
    "only_supported_pattern",
    "paid_bookings",
    "paid_revenue",
    "recency",
    "deterministic_tie_breaker",
]:
    if len(selected_candidates) == 1:
        return "only_supported_pattern"

    current_index = rank - 1
    current_candidate = selected_candidates[current_index]
    reference_candidate = (
        selected_candidates[1]
        if current_index == 0
        else selected_candidates[current_index - 1]
    )

    if current_candidate.paid_booking_count != reference_candidate.paid_booking_count:
        return "paid_bookings"
    if current_candidate.paid_revenue_cents != reference_candidate.paid_revenue_cents:
        return "paid_revenue"
    if current_candidate.last_paid_at != reference_candidate.last_paid_at:
        return "recency"
    return "deterministic_tie_breaker"


def _build_run_lineage(run_record: CreatorExperimentRunRecord) -> HelperGenerationLineage:
    return HelperGenerationLineage(
        generator_type=run_record.run_generator_type,
        model_name=run_record.run_model_name,
        prompt_version=run_record.run_prompt_version,
        config_version=run_record.run_config_version,
        contract_version=run_record.run_contract_version,
        reducer_version=run_record.run_reducer_version,
    )


def _build_card_record_lineage(
    card_record: CreatorExperimentRunCardRecord,
) -> HelperGenerationLineage | None:
    claim_snapshot = card_record.claim_snapshot
    if claim_snapshot is None:
        return None
    return _build_claim_lineage(claim_snapshot)


def _build_claim_lineage(claim_snapshot) -> HelperGenerationLineage:
    return HelperGenerationLineage(
        generator_type=claim_snapshot.claim_generator_type,
        model_name=claim_snapshot.claim_model_name,
        prompt_version=claim_snapshot.claim_prompt_version,
        config_version=claim_snapshot.claim_config_version,
        contract_version=claim_snapshot.claim_contract_version,
        reducer_version=claim_snapshot.claim_reducer_version,
    )


def _build_version_semantics(
    lineage: HelperGenerationLineage,
) -> HelperVersionSemantics:
    return HelperVersionSemantics(
        schema_version=EXPERIMENT_RESULT_SCHEMA_VERSION,
        evidence_input_version=EXPERIMENT_EVIDENCE_INPUT_VERSION,
        generation_config_version=lineage.config_version,
    )


def _build_experiment_card_id(*, candidate: _ExperimentCandidate) -> str:
    normalized_topic = candidate.primary_topic_label.casefold().strip()
    seed = f"{EXPERIMENT_CARD_ID_VERSION}:{candidate.content.id}:{normalized_topic}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]
    return f"ncexpcard_{digest}"


def _card_comparison_key(card: NextContentExperimentCard) -> tuple[str, str]:
    if card.card_id is not None:
        return ("card_id", card.card_id)
    if card.card_order is not None:
        return ("legacy_order", str(card.card_order))
    return ("legacy_tid", card.content_tids[0])


def _comparison_card_id(
    baseline_card: NextContentExperimentCard | None,
    candidate_card: NextContentExperimentCard | None,
) -> str | None:
    if baseline_card is not None and baseline_card.card_id is not None:
        return baseline_card.card_id
    if candidate_card is not None:
        return candidate_card.card_id
    return None


def _format_revenue_summary(rows: list[SettledPaidEvidenceRow]) -> str:
    totals_by_currency: dict[str, int] = defaultdict(int)
    for row in rows:
        totals_by_currency[row.invoice_currency] += row.invoice_amount_cents

    parts = [
        f"{currency} {_format_money_from_cents(amount_cents)}"
        for currency, amount_cents in sorted(totals_by_currency.items())
    ]
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts)


def _format_money_from_cents(amount_cents: int) -> str:
    amount = Decimal(amount_cents) / Decimal("100")
    return f"{amount:,.2f}"


def _ready_summary(*, card_count: int) -> str:
    if card_count == 1:
        return (
            "Here is the next content experiment most grounded in your current "
            "authoritative topics and attributed paid results."
        )
    return (
        "Here are the next content experiments most grounded in your current "
        "authoritative topics and attributed paid results."
    )


def _rendered_claim_text(*, card: NextContentExperimentCard) -> str:
    return f"{card.title}\n{card.hypothesis}"


def _truncate_title(value: str) -> str:
    if len(value) <= 255:
        return value
    return value[:252].rstrip() + "..."


def _validate_experiment_result(
    *,
    status: str,
    summary: str,
    experiments: list[NextContentExperimentCard],
) -> None:
    if status == EXPERIMENT_RUN_STATUS_UNSUPPORTED:
        if summary != UNSUPPORTED_EXPERIMENTS_SUMMARY:
            raise ValueError("unsupported experiment runs must use the exact unsupported summary")
        if experiments:
            raise ValueError("unsupported experiment runs must not include experiment cards")
        return

    if status != EXPERIMENT_RUN_STATUS_READY:
        raise ValueError("experiment run status must be ready or unsupported")
    if not 1 <= len(experiments) <= MAX_EXPERIMENT_CARDS:
        raise ValueError("ready experiment runs must include between 1 and 3 cards")
    for experiment in experiments:
        _validate_experiment_card(experiment)


def _validate_experiment_card(experiment: NextContentExperimentCard) -> None:
    if experiment.card_id is not None and not experiment.card_id.strip():
        raise ValueError("experiment card ids must be non-empty when recorded")
    if experiment.ranking_rationale is not None and not experiment.ranking_rationale.strip():
        raise ValueError("experiment ranking rationale must be non-empty when recorded")
    if not experiment.title.strip():
        raise ValueError("experiment title is required")
    if not experiment.hypothesis.startswith("Test whether "):
        raise ValueError("experiment hypothesis must use hypothesis-style wording")
    if not experiment.caution.startswith("Treat this as a hypothesis"):
        raise ValueError("experiment caution must keep hypothesis-style framing")
    if len(experiment.content_tids) != 1:
        raise ValueError("Story 65 experiment cards must be single-content-backed")
    if not all(tid.strip() for tid in experiment.content_tids):
        raise ValueError("experiment cards must include non-empty content tids")
