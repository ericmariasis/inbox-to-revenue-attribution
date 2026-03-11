from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.models.blocked_billing_case import BlockedBillingCase
from app.models.booking import Booking
from app.models.content import Content
from app.models.invoice import Invoice
from app.models.invoice_payment_event import InvoicePaymentEvent
from app.services.invoice_payment_events import ATTRIBUTED_PAYMENT_EVENT_STATUSES

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
    stripe_invoice_id: str
    invoice_amount_cents: int
    invoice_currency: str
    invoice_paid_at: datetime
    payment_event_id: UUID | None
    stripe_event_id: str | None
    payment_event_status: str | None
    payment_event_paid_at: datetime | None
    payment_event_received_at: datetime | None


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

    settled_rows: list[SettledPaidEvidenceRow] = []
    seen_invoice_ids: set[UUID] = set()
    for row in settled_row_values:
        invoice_id = row[7]
        if invoice_id in seen_invoice_ids:
            continue
        seen_invoice_ids.add(invoice_id)
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
                stripe_invoice_id=row[8],
                invoice_amount_cents=row[9],
                invoice_currency=row[10],
                invoice_paid_at=row[11],
                payment_event_id=row[12],
                stripe_event_id=row[13],
                payment_event_status=row[14],
                payment_event_paid_at=row[15],
                payment_event_received_at=row[16],
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
            Invoice.stripe_invoice_id,
            Invoice.amount_cents,
            Invoice.currency,
            Invoice.paid_at,
            InvoicePaymentEvent.id,
            InvoicePaymentEvent.stripe_event_id,
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
