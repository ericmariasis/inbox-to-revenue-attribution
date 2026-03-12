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
from app.services.creator_claim_snapshots import (
    CreateCreatorClaimSnapshotInput,
    create_creator_claim_snapshot,
)
from app.services.settled_paid_evidence import (
    SettledPaidEvidenceRow,
    get_creator_settled_paid_evidence,
)

EXPERIMENT_RUN_STATUS_READY = "ready"
EXPERIMENT_RUN_STATUS_UNSUPPORTED = "unsupported"
EXPERIMENT_RUN_CONTRACT_VERSION = "next_content_experiments_helper.v1"
EXPERIMENT_RUN_REDUCER_VERSION = "next_content_experiments.rules.v1"
EXPERIMENT_CARD_CLAIM_KIND = "next_content_experiment_card"
EXPERIMENT_CARD_CLAIM_CONTRACT_VERSION = "next_content_experiment_card.v1"
EXPERIMENT_CARD_CLAIM_REDUCER_VERSION = EXPERIMENT_RUN_REDUCER_VERSION
MAX_EXPERIMENT_CARDS = 3
UNSUPPORTED_EXPERIMENTS_SUMMARY = (
    "Not enough trusted evidence yet to suggest next content experiments. "
    "Finish reviewing content topics or wait for more attributed paid results."
)

ExperimentRunStatus = Literal["ready", "unsupported"]


@dataclass(frozen=True)
class NextContentExperimentCard:
    title: str
    hypothesis: str
    why_this_might_work: str
    evidence_summary: str
    content_tids: list[str]
    caution: str


@dataclass(frozen=True)
class CreatorNextContentExperimentsResult:
    claim_snapshot_id: UUID
    status: ExperimentRunStatus
    summary: str
    experiments: list[NextContentExperimentCard]
    created_at: datetime


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


def create_creator_next_content_experiments_run(
    *,
    creator_id: UUID,
    db: Session,
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
        run_contract_version=EXPERIMENT_RUN_CONTRACT_VERSION,
        run_reducer_version=EXPERIMENT_RUN_REDUCER_VERSION,
        run_prompt_version=None,
    )
    db.add(run_record)
    db.flush()

    if candidates:
        selected_candidates = sorted(candidates, key=_candidate_sort_key)[:MAX_EXPERIMENT_CARDS]
        cards: list[NextContentExperimentCard] = []
        run_record.status = EXPERIMENT_RUN_STATUS_READY
        run_record.summary_text = _ready_summary(card_count=len(selected_candidates))

        for index, candidate in enumerate(selected_candidates, start=1):
            card = _build_experiment_card(candidate=candidate)
            claim_snapshot = create_creator_claim_snapshot(
                creator_id=creator_id,
                input=CreateCreatorClaimSnapshotInput(
                    claim_kind=EXPERIMENT_CARD_CLAIM_KIND,
                    content_id=candidate.content.id,
                    authoritative_extraction_artifact_id=candidate.authoritative_evidence.artifact.id,
                    authoritative_fetch_snapshot_id=candidate.authoritative_evidence.fetch_snapshot.id,
                    settled_paid_evidence_rows=candidate.settled_paid_rows,
                    claim_contract_version=EXPERIMENT_CARD_CLAIM_CONTRACT_VERSION,
                    claim_reducer_version=EXPERIMENT_CARD_CLAIM_REDUCER_VERSION,
                    rendered_claim_text=_rendered_claim_text(card=card),
                ),
                db=db,
            )
            run_record.cards.append(
                CreatorExperimentRunCardRecord(
                    claim_snapshot_id=claim_snapshot.id,
                    content_tid=card.content_tids[0],
                    title=card.title,
                    hypothesis=card.hypothesis,
                    why_this_might_work=card.why_this_might_work,
                    evidence_summary=card.evidence_summary,
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
    return _build_experiment_result(run_record)


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


def _creator_experiment_run_query(*, creator_id: UUID):
    return (
        select(CreatorExperimentRunRecord)
        .options(selectinload(CreatorExperimentRunRecord.cards))
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
            title=card.title,
            hypothesis=card.hypothesis,
            why_this_might_work=card.why_this_might_work,
            evidence_summary=card.evidence_summary,
            content_tids=[card.content_tid],
            caution=card.caution,
        )
        for card in run_record.cards
    ]
    _validate_experiment_result(
        status=run_record.status,
        summary=run_record.summary_text,
        experiments=experiments,
    )
    return CreatorNextContentExperimentsResult(
        claim_snapshot_id=run_record.id,
        status=run_record.status,
        summary=run_record.summary_text,
        experiments=experiments,
        created_at=run_record.created_at,
    )


def _build_experiment_card(
    *,
    candidate: _ExperimentCandidate,
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
    )


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
