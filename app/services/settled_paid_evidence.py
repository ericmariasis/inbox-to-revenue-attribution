from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.models.billing_provider import BILLING_PROVIDER_STRIPE
from app.models.blocked_billing_case import BlockedBillingCase
from app.models.booking import Booking
from app.models.content import Content
from app.models.invoice import Invoice
from app.models.invoice_payment_event import InvoicePaymentEvent
from app.services.invoice_payment_events import (
    ATTRIBUTED_PAYMENT_EVENT_STATUSES,
    PaymentProvenanceSummary,
    build_payment_provenance_summary,
)

CURRENT_UNMATCHED_PAYMENT_BACKLOG_SCOPE = "current_backlog"
CURRENT_BLOCKED_BILLING_BACKLOG_SCOPE = "current_backlog"


@dataclass(frozen=True)
class SettledPaidEvidenceRow:
    content_id: UUID
    booking_link_id: UUID
    tid: str
    source_url: str
    booking_id: UUID
    booking_uuid: str
    booked_at: datetime
    invoice_id: UUID
    payment_provider: str
    provider_invoice_id: str
    invoice_amount_cents: int
    invoice_currency: str
    invoice_paid_at: datetime
    payment_event_id: UUID | None
    provider_event_id: str | None
    payment_event_status: str | None
    payment_event_paid_at: datetime | None
    payment_event_received_at: datetime | None
    payment_provenance: PaymentProvenanceSummary

    @property
    def stripe_invoice_id(self) -> str | None:
        if self.payment_provider == BILLING_PROVIDER_STRIPE:
            return self.provider_invoice_id
        return None

    @property
    def stripe_event_id(self) -> str | None:
        if self.payment_provider == BILLING_PROVIDER_STRIPE:
            return self.provider_event_id
        return None


@dataclass(frozen=True)
class SettledPaidEvidenceReference:
    content_id: UUID
    booking_id: UUID
    invoice_id: UUID
    payment_event_id: UUID | None


@dataclass(frozen=True)
class UnmatchedPaymentBacklogReasonCount:
    reason: str | None
    event_count: int


@dataclass(frozen=True)
class UnmatchedPaymentBacklog:
    scope: str
    event_count: int
    reasons: list[UnmatchedPaymentBacklogReasonCount]


@dataclass(frozen=True)
class BlockedBillingBacklogReasonCount:
    reason_code: str
    case_count: int


@dataclass(frozen=True)
class BlockedBillingBacklog:
    scope: str
    open_case_count: int
    reasons: list[BlockedBillingBacklogReasonCount]


@dataclass(frozen=True)
class _ProviderInvoiceConflictSummary:
    event_count: int
    reasons: tuple[str | None, ...]


@dataclass(frozen=True)
class CreatorSettledPaidEvidenceSnapshot:
    creator_id: UUID
    start_date: date | None
    end_date: date | None
    settled_rows: list[SettledPaidEvidenceRow]
    unmatched_payment_backlog: UnmatchedPaymentBacklog
    blocked_billing_backlog: BlockedBillingBacklog


def get_creator_settled_paid_evidence(
    *,
    creator_id: UUID,
    db: Session,
    start_date: date | None = None,
    end_date: date | None = None,
    tid: str | None = None,
) -> CreatorSettledPaidEvidenceSnapshot:
    _validate_date_range(start_date=start_date, end_date=end_date)

    settled_row_values = db.execute(
        _creator_settled_paid_evidence_query(
            creator_id=creator_id,
            start_date=start_date,
            end_date=end_date,
            tid=tid,
        )
    ).all()
    unmatched_rows = db.execute(
        _creator_unmatched_payment_backlog_query(creator_id=creator_id)
    ).all()
    blocked_rows = db.execute(
        _creator_blocked_billing_backlog_query(creator_id=creator_id)
    ).all()
    conflicts_by_provider_invoice_identity = _load_unmatched_payment_conflicts_by_provider_invoice_identity(
        creator_id=creator_id,
        provider_invoice_identities=[
            (row[8], row[9])
            for row in settled_row_values
            if row[9] is not None
        ],
        db=db,
    )

    settled_rows: list[SettledPaidEvidenceRow] = []
    seen_invoice_ids: set[UUID] = set()
    for row in settled_row_values:
        invoice_id = row[7]
        if invoice_id in seen_invoice_ids:
            continue
        seen_invoice_ids.add(invoice_id)
        conflict_summary = conflicts_by_provider_invoice_identity.get((row[8], row[9]))
        settled_rows.append(
            SettledPaidEvidenceRow(
                content_id=row[0],
                booking_link_id=row[1],
                tid=row[2],
                source_url=row[3],
                booking_id=row[4],
                booking_uuid=row[5],
                booked_at=row[6],
                invoice_id=row[7],
                payment_provider=row[8],
                provider_invoice_id=row[9],
                invoice_amount_cents=row[10],
                invoice_currency=row[11],
                invoice_paid_at=row[12],
                payment_event_id=row[13],
                provider_event_id=row[14],
                payment_event_status=row[15],
                payment_event_paid_at=row[16],
                payment_event_received_at=row[17],
                payment_provenance=build_payment_provenance_summary(
                    payment_event_status=row[15],
                    conflict_event_count=(
                        conflict_summary.event_count if conflict_summary is not None else 0
                    ),
                    conflict_reasons=(
                        conflict_summary.reasons if conflict_summary is not None else ()
                    ),
                ),
            )
        )

    unmatched_reasons = [
        UnmatchedPaymentBacklogReasonCount(reason=reason, event_count=event_count)
        for reason, event_count in unmatched_rows
    ]
    blocked_reasons = [
        BlockedBillingBacklogReasonCount(reason_code=reason_code, case_count=case_count)
        for reason_code, case_count in blocked_rows
    ]

    return CreatorSettledPaidEvidenceSnapshot(
        creator_id=creator_id,
        start_date=start_date,
        end_date=end_date,
        settled_rows=settled_rows,
        unmatched_payment_backlog=UnmatchedPaymentBacklog(
            scope=CURRENT_UNMATCHED_PAYMENT_BACKLOG_SCOPE,
            event_count=sum(item.event_count for item in unmatched_reasons),
            reasons=unmatched_reasons,
        ),
        blocked_billing_backlog=BlockedBillingBacklog(
            scope=CURRENT_BLOCKED_BILLING_BACKLOG_SCOPE,
            open_case_count=sum(item.case_count for item in blocked_reasons),
            reasons=blocked_reasons,
        ),
    )


def build_settled_paid_evidence_reference(
    row: SettledPaidEvidenceRow,
) -> SettledPaidEvidenceReference:
    return SettledPaidEvidenceReference(
        content_id=row.content_id,
        booking_id=row.booking_id,
        invoice_id=row.invoice_id,
        payment_event_id=row.payment_event_id,
    )


def get_creator_settled_paid_evidence_rows_for_references(
    *,
    creator_id: UUID,
    references: Sequence[SettledPaidEvidenceReference],
    db: Session,
) -> list[SettledPaidEvidenceRow]:
    ordered_references = list(references)
    if not ordered_references:
        return []

    invoice_ids = sorted({reference.invoice_id for reference in ordered_references}, key=str)
    base_rows = db.execute(
        _creator_settled_paid_evidence_query_for_invoice_ids(
            creator_id=creator_id,
            invoice_ids=invoice_ids,
        )
    ).all()
    base_rows_by_invoice_id = {row[7]: row for row in base_rows}

    payment_event_ids = [
        reference.payment_event_id
        for reference in ordered_references
        if reference.payment_event_id is not None
    ]
    payment_events_by_id: dict[UUID, InvoicePaymentEvent] = {}
    if payment_event_ids:
        payment_event_rows = db.execute(
            select(InvoicePaymentEvent).where(
                InvoicePaymentEvent.id.in_(payment_event_ids),
                InvoicePaymentEvent.creator_id == creator_id,
            )
        ).scalars().all()
        payment_events_by_id = {event.id: event for event in payment_event_rows}
    conflicts_by_provider_invoice_identity = _load_unmatched_payment_conflicts_by_provider_invoice_identity(
        creator_id=creator_id,
        provider_invoice_identities=[
            (row[8], row[9])
            for row in base_rows
            if row[9] is not None
        ],
        db=db,
    )

    resolved_rows: list[SettledPaidEvidenceRow] = []
    for reference in ordered_references:
        row = base_rows_by_invoice_id.get(reference.invoice_id)
        if row is None:
            continue
        if row[0] != reference.content_id or row[4] != reference.booking_id:
            continue

        payment_event = (
            payment_events_by_id.get(reference.payment_event_id)
            if reference.payment_event_id is not None
            else None
        )
        if reference.payment_event_id is not None and payment_event is None:
            continue
        if payment_event is not None and payment_event.invoice_id != reference.invoice_id:
            continue

        conflict_summary = conflicts_by_provider_invoice_identity.get((row[8], row[9]))
        resolved_rows.append(
            SettledPaidEvidenceRow(
                content_id=row[0],
                booking_link_id=row[1],
                tid=row[2],
                source_url=row[3],
                booking_id=row[4],
                booking_uuid=row[5],
                booked_at=row[6],
                invoice_id=row[7],
                payment_provider=row[8],
                provider_invoice_id=row[9],
                invoice_amount_cents=row[10],
                invoice_currency=row[11],
                invoice_paid_at=row[12],
                payment_event_id=payment_event.id if payment_event is not None else None,
                provider_event_id=(
                    payment_event.resolved_provider_event_id
                    if payment_event is not None
                    else None
                ),
                payment_event_status=payment_event.status if payment_event is not None else None,
                payment_event_paid_at=payment_event.paid_at if payment_event is not None else None,
                payment_event_received_at=payment_event.received_at if payment_event is not None else None,
                payment_provenance=build_payment_provenance_summary(
                    payment_event_status=payment_event.status if payment_event is not None else None,
                    conflict_event_count=(
                        conflict_summary.event_count if conflict_summary is not None else 0
                    ),
                    conflict_reasons=(
                        conflict_summary.reasons if conflict_summary is not None else ()
                    ),
                ),
            )
        )

    return resolved_rows


def _creator_settled_paid_evidence_query(
    *,
    creator_id: UUID,
    start_date: date | None,
    end_date: date | None,
    tid: str | None = None,
):
    filters = [
        Invoice.creator_id == creator_id,
        Invoice.status == "paid",
        Invoice.paid_at.is_not(None),
        Booking.creator_id == creator_id,
        Booking.tid.is_not(None),
        *_paid_date_filters(start_date=start_date, end_date=end_date),
    ]
    if tid is not None:
        filters.append(Booking.tid == tid)

    return (
        select(
            Content.id,
            Content.booking_link_id,
            Booking.tid,
            Content.source_url,
            Booking.id,
            Booking.calendly_booking_uuid,
            Booking.booked_at,
            Invoice.id,
            Invoice.payment_provider,
            func.coalesce(Invoice.provider_invoice_id, Invoice.stripe_invoice_id),
            Invoice.amount_cents,
            Invoice.currency,
            Invoice.paid_at,
            InvoicePaymentEvent.id,
            func.coalesce(
                InvoicePaymentEvent.provider_event_id,
                InvoicePaymentEvent.stripe_event_id,
            ),
            InvoicePaymentEvent.status,
            InvoicePaymentEvent.paid_at,
            InvoicePaymentEvent.received_at,
        )
        .select_from(Invoice)
        .join(Booking, Booking.id == Invoice.booking_id)
        .join(
            Content,
            and_(
                Content.tid == Booking.tid,
                Content.creator_id == creator_id,
            ),
        )
        .outerjoin(
            InvoicePaymentEvent,
            and_(
                InvoicePaymentEvent.invoice_id == Invoice.id,
                InvoicePaymentEvent.creator_id == creator_id,
                InvoicePaymentEvent.status.in_(ATTRIBUTED_PAYMENT_EVENT_STATUSES),
            ),
        )
        .where(*filters)
        .order_by(
            Invoice.paid_at.desc(),
            Booking.booked_at.desc(),
            InvoicePaymentEvent.received_at.desc().nullslast(),
        )
    )


def _creator_settled_paid_evidence_query_for_invoice_ids(
    *,
    creator_id: UUID,
    invoice_ids: Sequence[UUID],
):
    return (
        select(
            Content.id,
            Content.booking_link_id,
            Booking.tid,
            Content.source_url,
            Booking.id,
            Booking.calendly_booking_uuid,
            Booking.booked_at,
            Invoice.id,
            Invoice.payment_provider,
            func.coalesce(Invoice.provider_invoice_id, Invoice.stripe_invoice_id),
            Invoice.amount_cents,
            Invoice.currency,
            Invoice.paid_at,
        )
        .select_from(Invoice)
        .join(Booking, Booking.id == Invoice.booking_id)
        .join(
            Content,
            and_(
                Content.tid == Booking.tid,
                Content.creator_id == creator_id,
            ),
        )
        .where(
            Invoice.creator_id == creator_id,
            Invoice.id.in_(invoice_ids),
            Invoice.status == "paid",
            Invoice.paid_at.is_not(None),
            Booking.creator_id == creator_id,
            Booking.tid.is_not(None),
        )
        .order_by(Invoice.paid_at.desc(), Booking.booked_at.desc())
    )


def _creator_unmatched_payment_backlog_query(*, creator_id: UUID):
    return (
        select(
            InvoicePaymentEvent.unattributed_reason,
            func.count(InvoicePaymentEvent.id),
        )
        .where(
            InvoicePaymentEvent.creator_id == creator_id,
            InvoicePaymentEvent.status == "unmatched",
        )
        .group_by(InvoicePaymentEvent.unattributed_reason)
        .order_by(InvoicePaymentEvent.unattributed_reason.asc())
    )


def _creator_unmatched_payment_conflicts_query(
    *,
    creator_id: UUID,
    provider_invoice_ids: Sequence[str],
):
    return (
        select(
            InvoicePaymentEvent.payment_provider,
            func.coalesce(
                InvoicePaymentEvent.provider_invoice_id,
                InvoicePaymentEvent.stripe_invoice_id,
            ),
            InvoicePaymentEvent.unattributed_reason,
            func.count(InvoicePaymentEvent.id),
        )
        .where(
            InvoicePaymentEvent.creator_id == creator_id,
            InvoicePaymentEvent.status == "unmatched",
            func.coalesce(
                InvoicePaymentEvent.provider_invoice_id,
                InvoicePaymentEvent.stripe_invoice_id,
            ).in_(provider_invoice_ids),
        )
        .group_by(
            InvoicePaymentEvent.payment_provider,
            func.coalesce(
                InvoicePaymentEvent.provider_invoice_id,
                InvoicePaymentEvent.stripe_invoice_id,
            ),
            InvoicePaymentEvent.unattributed_reason,
        )
        .order_by(
            InvoicePaymentEvent.payment_provider.asc(),
            func.coalesce(
                InvoicePaymentEvent.provider_invoice_id,
                InvoicePaymentEvent.stripe_invoice_id,
            ).asc(),
            InvoicePaymentEvent.unattributed_reason.asc(),
        )
    )


def _creator_blocked_billing_backlog_query(*, creator_id: UUID):
    return (
        select(
            BlockedBillingCase.reason_code,
            func.count(BlockedBillingCase.id),
        )
        .where(
            BlockedBillingCase.creator_id == creator_id,
            BlockedBillingCase.status == "open",
        )
        .group_by(BlockedBillingCase.reason_code)
        .order_by(BlockedBillingCase.reason_code.asc())
    )


def _load_unmatched_payment_conflicts_by_provider_invoice_identity(
    *,
    creator_id: UUID,
    provider_invoice_identities: Sequence[tuple[str, str]],
    db: Session,
) -> dict[tuple[str, str], _ProviderInvoiceConflictSummary]:
    deduped_invoice_ids = sorted(
        {provider_invoice_id for _, provider_invoice_id in provider_invoice_identities}
    )
    if not deduped_invoice_ids:
        return {}

    rows = db.execute(
        _creator_unmatched_payment_conflicts_query(
            creator_id=creator_id,
            provider_invoice_ids=deduped_invoice_ids,
        )
    ).all()
    grouped_counts: dict[tuple[str, str], int] = {}
    grouped_reasons: dict[tuple[str, str], list[str | None]] = {}

    for payment_provider, provider_invoice_id, reason, event_count in rows:
        if provider_invoice_id is None:
            continue
        key = (payment_provider, provider_invoice_id)
        grouped_counts[key] = grouped_counts.get(key, 0) + event_count
        grouped_reasons.setdefault(key, []).append(reason)

    return {
        key: _ProviderInvoiceConflictSummary(
            event_count=grouped_counts[key],
            reasons=tuple(grouped_reasons.get(key, [])),
        )
        for key in grouped_counts
    }


def _paid_date_filters(
    *,
    start_date: date | None,
    end_date: date | None,
) -> tuple[object, ...]:
    filters: list[object] = []
    if start_date is not None:
        filters.append(Invoice.paid_at >= _start_of_day(start_date))
    if end_date is not None:
        filters.append(Invoice.paid_at < _start_of_day(end_date + timedelta(days=1)))
    return tuple(filters)


def _start_of_day(value: date) -> datetime:
    return datetime.combine(value, time.min, tzinfo=UTC)


def _validate_date_range(*, start_date: date | None, end_date: date | None) -> None:
    if start_date is not None and end_date is not None and start_date > end_date:
        raise ValueError("start_date must be on or before end_date")
