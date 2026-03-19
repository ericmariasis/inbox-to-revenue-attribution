from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.models.booking import Booking
from app.models.booking_provider import BOOKING_PROVIDER_FULLSCOPE
from app.models.calendly_webhook_event import CalendlyWebhookEventRecord
from app.models.content import Content
from app.models.content_extraction_artifact import ContentExtractionArtifact
from app.models.content_topic_candidate import ContentTopicCandidate
from app.models.fullscope_webhook_event import FullScopeWebhookEventRecord
from app.services.booking_attribution import (
    BOOKING_ATTRIBUTION_STATUS_UNATTRIBUTED,
    BOOKING_UNATTRIBUTED_REASON_MISSING_TID,
    BOOKING_UNATTRIBUTED_REASON_UNKNOWN_TID,
)
from app.services.content_topics import CONTENT_TOPIC_REVIEW_STATUS_PENDING
from app.services.invoice_payment_events import (
    PAYMENT_PROVENANCE_STATE_CONFLICTING,
    PAYMENT_PROVENANCE_STATE_MATCHED,
    PAYMENT_PROVENANCE_STATE_PENDING,
    PAYMENT_PROVENANCE_STATE_UNMATCHED,
)
from app.services.settled_paid_evidence import get_creator_settled_paid_evidence

PROVIDER_INGRESS_BACKLOG_PROCESSING_STATUSES = (
    "received",
    "processing",
    "deferred_missing_booking",
)
PROVIDER_INGRESS_FAILURE_PROCESSING_STATUSES = ("failed",)
PROVIDER_INGRESS_HEALTH_PROCESSING_STATUSES = (
    *PROVIDER_INGRESS_BACKLOG_PROCESSING_STATUSES,
    *PROVIDER_INGRESS_FAILURE_PROCESSING_STATUSES,
)
PAYMENT_PROVENANCE_STATE_ORDER = (
    PAYMENT_PROVENANCE_STATE_MATCHED,
    PAYMENT_PROVENANCE_STATE_PENDING,
    PAYMENT_PROVENANCE_STATE_UNMATCHED,
    PAYMENT_PROVENANCE_STATE_CONFLICTING,
)
AUTHORITATIVE_CONTENT_LAG_REASON_MISSING_AUTHORITY = "missing_authoritative_evidence"
AUTHORITATIVE_CONTENT_LAG_REASON_STALE_AUTHORITY = "stale_authoritative_evidence"
AUTHORITATIVE_CONTENT_LAG_REASON_ORDER = (
    AUTHORITATIVE_CONTENT_LAG_REASON_MISSING_AUTHORITY,
    AUTHORITATIVE_CONTENT_LAG_REASON_STALE_AUTHORITY,
)


@dataclass(frozen=True)
class BookingAttributionReasonCount:
    reason: str
    booking_count: int


@dataclass(frozen=True)
class BookingAttributionHealthSnapshot:
    unattributed_booking_count: int
    reasons: list[BookingAttributionReasonCount]


@dataclass(frozen=True)
class ProviderIngressStatusCount:
    processing_status: str
    event_count: int


@dataclass(frozen=True)
class ProviderIngressHealthSnapshot:
    backlog_event_count: int
    failed_event_count: int
    statuses: list[ProviderIngressStatusCount]


@dataclass(frozen=True)
class PaymentProvenanceStateCount:
    state: str
    row_count: int


@dataclass(frozen=True)
class PaymentProvenanceReasonCount:
    reason: str | None
    event_count: int


@dataclass(frozen=True)
class PaymentProvenanceHealthSnapshot:
    settled_state_counts: list[PaymentProvenanceStateCount]
    current_backlog_event_count: int
    current_backlog_reasons: list[PaymentProvenanceReasonCount]


@dataclass(frozen=True)
class BlockedBillingReasonCount:
    reason_code: str
    case_count: int


@dataclass(frozen=True)
class BlockedBillingHealthSnapshot:
    open_case_count: int
    reasons: list[BlockedBillingReasonCount]


@dataclass(frozen=True)
class AuthoritativeContentLagReasonCount:
    reason: str
    content_count: int


@dataclass(frozen=True)
class AuthoritativeContentHealthSnapshot:
    lagging_content_count: int
    reasons: list[AuthoritativeContentLagReasonCount]


@dataclass(frozen=True)
class CreatorEvidenceIngressHealthSnapshot:
    creator_id: UUID
    booking_attribution: BookingAttributionHealthSnapshot
    calendly_ingress: ProviderIngressHealthSnapshot
    fullscope_ingress: ProviderIngressHealthSnapshot
    payment_provenance: PaymentProvenanceHealthSnapshot
    blocked_billing: BlockedBillingHealthSnapshot
    authoritative_content: AuthoritativeContentHealthSnapshot


def get_creator_evidence_ingress_health_snapshot(
    *,
    creator_id: UUID,
    db: Session,
) -> CreatorEvidenceIngressHealthSnapshot:
    settled_snapshot = get_creator_settled_paid_evidence(
        creator_id=creator_id,
        db=db,
    )

    return CreatorEvidenceIngressHealthSnapshot(
        creator_id=creator_id,
        booking_attribution=_build_booking_attribution_health_snapshot(
            creator_id=creator_id,
            db=db,
        ),
        calendly_ingress=_build_calendly_ingress_health_snapshot(
            creator_id=creator_id,
            db=db,
        ),
        fullscope_ingress=_build_fullscope_ingress_health_snapshot(
            creator_id=creator_id,
            db=db,
        ),
        payment_provenance=_build_payment_provenance_health_snapshot(
            settled_snapshot=settled_snapshot,
        ),
        blocked_billing=_build_blocked_billing_health_snapshot(
            settled_snapshot=settled_snapshot,
        ),
        authoritative_content=_build_authoritative_content_health_snapshot(
            creator_id=creator_id,
            db=db,
        ),
    )


def _build_booking_attribution_health_snapshot(
    *,
    creator_id: UUID,
    db: Session,
) -> BookingAttributionHealthSnapshot:
    rows = db.execute(
        select(
            Booking.unattributed_reason,
            func.count(Booking.id),
        )
        .where(
            Booking.creator_id == creator_id,
            Booking.attribution_status == BOOKING_ATTRIBUTION_STATUS_UNATTRIBUTED,
        )
        .group_by(Booking.unattributed_reason)
    ).all()
    counts_by_reason = {reason: booking_count for reason, booking_count in rows}
    reasons = [
        BookingAttributionReasonCount(
            reason=reason,
            booking_count=counts_by_reason.get(reason, 0),
        )
        for reason in (
            BOOKING_UNATTRIBUTED_REASON_MISSING_TID,
            BOOKING_UNATTRIBUTED_REASON_UNKNOWN_TID,
        )
    ]
    return BookingAttributionHealthSnapshot(
        unattributed_booking_count=sum(item.booking_count for item in reasons),
        reasons=reasons,
    )


def _build_calendly_ingress_health_snapshot(
    *,
    creator_id: UUID,
    db: Session,
) -> ProviderIngressHealthSnapshot:
    rows = db.execute(
        select(
            CalendlyWebhookEventRecord.processing_status,
            func.count(CalendlyWebhookEventRecord.id),
        )
        .select_from(CalendlyWebhookEventRecord)
        .outerjoin(
            Content,
            and_(
                Content.tid == CalendlyWebhookEventRecord.tid,
                Content.creator_id == creator_id,
            ),
        )
        .outerjoin(
            Booking,
            and_(
                Booking.calendly_booking_uuid
                == CalendlyWebhookEventRecord.calendly_booking_uuid,
                Booking.creator_id == creator_id,
            ),
        )
        .where(
            CalendlyWebhookEventRecord.processing_status.in_(
                PROVIDER_INGRESS_HEALTH_PROCESSING_STATUSES
            ),
            or_(
                Content.id.is_not(None),
                Booking.id.is_not(None),
            ),
        )
        .group_by(CalendlyWebhookEventRecord.processing_status)
    ).all()
    return _build_provider_ingress_health_snapshot(
        counts_by_status={status: event_count for status, event_count in rows}
    )


def _build_fullscope_ingress_health_snapshot(
    *,
    creator_id: UUID,
    db: Session,
) -> ProviderIngressHealthSnapshot:
    rows = db.execute(
        select(
            FullScopeWebhookEventRecord.processing_status,
            func.count(FullScopeWebhookEventRecord.id),
        )
        .select_from(FullScopeWebhookEventRecord)
        .outerjoin(
            Content,
            and_(
                Content.tid == FullScopeWebhookEventRecord.tid,
                Content.creator_id == creator_id,
            ),
        )
        .outerjoin(
            Booking,
            and_(
                Booking.provider == BOOKING_PROVIDER_FULLSCOPE,
                Booking.provider_booking_id == FullScopeWebhookEventRecord.appointment_id,
                Booking.creator_id == creator_id,
            ),
        )
        .where(
            FullScopeWebhookEventRecord.processing_status.in_(
                PROVIDER_INGRESS_HEALTH_PROCESSING_STATUSES
            ),
            or_(
                Content.id.is_not(None),
                Booking.id.is_not(None),
            ),
        )
        .group_by(FullScopeWebhookEventRecord.processing_status)
    ).all()
    return _build_provider_ingress_health_snapshot(
        counts_by_status={status: event_count for status, event_count in rows}
    )


def _build_provider_ingress_health_snapshot(
    *,
    counts_by_status: dict[str, int],
) -> ProviderIngressHealthSnapshot:
    statuses = [
        ProviderIngressStatusCount(
            processing_status=status,
            event_count=counts_by_status.get(status, 0),
        )
        for status in PROVIDER_INGRESS_HEALTH_PROCESSING_STATUSES
    ]
    return ProviderIngressHealthSnapshot(
        backlog_event_count=sum(
            counts_by_status.get(status, 0)
            for status in PROVIDER_INGRESS_BACKLOG_PROCESSING_STATUSES
        ),
        failed_event_count=sum(
            counts_by_status.get(status, 0)
            for status in PROVIDER_INGRESS_FAILURE_PROCESSING_STATUSES
        ),
        statuses=statuses,
    )


def _build_payment_provenance_health_snapshot(
    *,
    settled_snapshot,
) -> PaymentProvenanceHealthSnapshot:
    counts_by_state = {
        state: 0
        for state in PAYMENT_PROVENANCE_STATE_ORDER
    }
    for row in settled_snapshot.settled_rows:
        counts_by_state[row.payment_provenance.state] += 1

    state_counts = [
        PaymentProvenanceStateCount(
            state=state,
            row_count=counts_by_state[state],
        )
        for state in PAYMENT_PROVENANCE_STATE_ORDER
    ]
    backlog_reasons = [
        PaymentProvenanceReasonCount(
            reason=item.reason,
            event_count=item.event_count,
        )
        for item in settled_snapshot.unmatched_payment_backlog.reasons
    ]
    return PaymentProvenanceHealthSnapshot(
        settled_state_counts=state_counts,
        current_backlog_event_count=settled_snapshot.unmatched_payment_backlog.event_count,
        current_backlog_reasons=backlog_reasons,
    )


def _build_blocked_billing_health_snapshot(
    *,
    settled_snapshot,
) -> BlockedBillingHealthSnapshot:
    return BlockedBillingHealthSnapshot(
        open_case_count=settled_snapshot.blocked_billing_backlog.open_case_count,
        reasons=[
            BlockedBillingReasonCount(
                reason_code=item.reason_code,
                case_count=item.case_count,
            )
            for item in settled_snapshot.blocked_billing_backlog.reasons
        ],
    )


def _build_authoritative_content_health_snapshot(
    *,
    creator_id: UUID,
    db: Session,
) -> AuthoritativeContentHealthSnapshot:
    content_rows = db.execute(
        select(
            Content.id,
            Content.authoritative_extraction_artifact_id,
        ).where(Content.creator_id == creator_id)
    ).all()
    if not content_rows:
        return AuthoritativeContentHealthSnapshot(
            lagging_content_count=0,
            reasons=[
                AuthoritativeContentLagReasonCount(reason=reason, content_count=0)
                for reason in AUTHORITATIVE_CONTENT_LAG_REASON_ORDER
            ],
        )

    latest_artifacts_by_content_id: dict[UUID, ContentExtractionArtifact] = {}
    artifact_rows = db.execute(
        select(ContentExtractionArtifact)
        .where(ContentExtractionArtifact.creator_id == creator_id)
        .order_by(
            ContentExtractionArtifact.content_id.asc(),
            ContentExtractionArtifact.created_at.desc(),
            ContentExtractionArtifact.id.desc(),
        )
    ).scalars().all()
    for artifact in artifact_rows:
        latest_artifacts_by_content_id.setdefault(artifact.content_id, artifact)

    latest_artifact_ids = [artifact.id for artifact in latest_artifacts_by_content_id.values()]
    candidates_by_artifact_id: dict[UUID, list[ContentTopicCandidate]] = {
        artifact_id: []
        for artifact_id in latest_artifact_ids
    }
    if latest_artifact_ids:
        candidate_rows = db.execute(
            select(ContentTopicCandidate)
            .where(
                ContentTopicCandidate.creator_id == creator_id,
                ContentTopicCandidate.extraction_artifact_id.in_(latest_artifact_ids),
            )
            .order_by(
                ContentTopicCandidate.extraction_artifact_id.asc(),
                ContentTopicCandidate.candidate_rank.asc(),
                ContentTopicCandidate.created_at.asc(),
                ContentTopicCandidate.id.asc(),
            )
        ).scalars().all()
        for candidate in candidate_rows:
            candidates_by_artifact_id.setdefault(
                candidate.extraction_artifact_id,
                [],
            ).append(candidate)

    counts_by_reason = {
        reason: 0
        for reason in AUTHORITATIVE_CONTENT_LAG_REASON_ORDER
    }
    for content_id, authoritative_extraction_artifact_id in content_rows:
        latest_artifact = latest_artifacts_by_content_id.get(content_id)
        if latest_artifact is None:
            continue
        candidate_topics = candidates_by_artifact_id.get(latest_artifact.id, [])
        if not candidate_topics:
            continue
        if any(
            candidate.review_status == CONTENT_TOPIC_REVIEW_STATUS_PENDING
            for candidate in candidate_topics
        ):
            continue
        if authoritative_extraction_artifact_id == latest_artifact.id:
            continue

        lag_reason = (
            AUTHORITATIVE_CONTENT_LAG_REASON_MISSING_AUTHORITY
            if authoritative_extraction_artifact_id is None
            else AUTHORITATIVE_CONTENT_LAG_REASON_STALE_AUTHORITY
        )
        counts_by_reason[lag_reason] += 1

    reasons = [
        AuthoritativeContentLagReasonCount(
            reason=reason,
            content_count=counts_by_reason[reason],
        )
        for reason in AUTHORITATIVE_CONTENT_LAG_REASON_ORDER
    ]
    return AuthoritativeContentHealthSnapshot(
        lagging_content_count=sum(item.content_count for item in reasons),
        reasons=reasons,
    )
