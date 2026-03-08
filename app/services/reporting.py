import csv
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from io import StringIO
from uuid import UUID

from sqlalchemy import and_, distinct, func, select
from sqlalchemy.orm import Session

from app.models.booking import Booking
from app.models.content import Content
from app.models.invoice import Invoice
from app.models.invoice_payment_event import InvoicePaymentEvent
from app.services.invoice_payment_events import ATTRIBUTED_PAYMENT_EVENT_STATUSES

CURRENT_UNATTRIBUTED_BACKLOG_SCOPE = "current_backlog"
BLOCKED_REPORTING_UNSUPPORTED_REASON = (
    "Billing deferrals are not yet persisted in canonical reporting state."
)


@dataclass(frozen=True)
class ReportsSummaryRow:
    content_id: UUID
    booking_link_id: UUID
    tid: str
    source_url: str
    paid_revenue_cents: int
    paid_invoice_count: int
    paid_booking_count: int
    first_paid_at: datetime
    last_paid_at: datetime


@dataclass(frozen=True)
class ReportsUnattributedReasonCount:
    reason: str | None
    event_count: int


@dataclass(frozen=True)
class ReportsUnattributedBacklog:
    scope: str
    event_count: int
    reasons: list[ReportsUnattributedReasonCount]


@dataclass(frozen=True)
class ReportsBlockedSummary:
    supported: bool
    reason: str | None


@dataclass(frozen=True)
class CreatorReportsSummary:
    creator_id: UUID
    start_date: date | None
    end_date: date | None
    rows: list[ReportsSummaryRow]
    paid_revenue_cents: int
    paid_invoice_count: int
    paid_booking_count: int
    unattributed_current_backlog: ReportsUnattributedBacklog
    blocked_summary: ReportsBlockedSummary


@dataclass(frozen=True)
class PaidAttributionEvidence:
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
class CreatorPaidAttributionExplanation:
    creator_id: UUID
    start_date: date | None
    end_date: date | None
    summary_row: ReportsSummaryRow
    evidence: list[PaidAttributionEvidence]


def get_creator_reports_summary(
    *,
    creator_id: UUID,
    db: Session,
    start_date: date | None = None,
    end_date: date | None = None,
) -> CreatorReportsSummary:
    _validate_date_range(start_date=start_date, end_date=end_date)

    paid_rows = db.execute(
        _creator_paid_summary_query(
            creator_id=creator_id,
            start_date=start_date,
            end_date=end_date,
        )
    ).all()
    unattributed_rows = db.execute(
        _creator_unattributed_backlog_query(creator_id=creator_id)
    ).all()

    rows = [
        ReportsSummaryRow(
            content_id=content_id,
            booking_link_id=booking_link_id,
            tid=tid,
            source_url=source_url,
            paid_revenue_cents=paid_revenue_cents,
            paid_invoice_count=paid_invoice_count,
            paid_booking_count=paid_booking_count,
            first_paid_at=first_paid_at,
            last_paid_at=last_paid_at,
        )
        for (
            content_id,
            booking_link_id,
            tid,
            source_url,
            paid_revenue_cents,
            paid_invoice_count,
            paid_booking_count,
            first_paid_at,
            last_paid_at,
        ) in paid_rows
    ]
    unattributed_reasons = [
        ReportsUnattributedReasonCount(
            reason=reason,
            event_count=event_count,
        )
        for reason, event_count in unattributed_rows
    ]

    return CreatorReportsSummary(
        creator_id=creator_id,
        start_date=start_date,
        end_date=end_date,
        rows=rows,
        paid_revenue_cents=sum(row.paid_revenue_cents for row in rows),
        paid_invoice_count=sum(row.paid_invoice_count for row in rows),
        paid_booking_count=sum(row.paid_booking_count for row in rows),
        unattributed_current_backlog=ReportsUnattributedBacklog(
            scope=CURRENT_UNATTRIBUTED_BACKLOG_SCOPE,
            event_count=sum(item.event_count for item in unattributed_reasons),
            reasons=unattributed_reasons,
        ),
        blocked_summary=ReportsBlockedSummary(
            supported=False,
            reason=BLOCKED_REPORTING_UNSUPPORTED_REASON,
        ),
    )


def get_creator_paid_attribution_explanation(
    *,
    creator_id: UUID,
    tid: str,
    db: Session,
    start_date: date | None = None,
    end_date: date | None = None,
) -> CreatorPaidAttributionExplanation | None:
    _validate_date_range(start_date=start_date, end_date=end_date)

    summary_row_values = db.execute(
        _creator_paid_summary_query(
            creator_id=creator_id,
            start_date=start_date,
            end_date=end_date,
            tid=tid,
        )
    ).one_or_none()
    if summary_row_values is None:
        return None

    summary_row = ReportsSummaryRow(
        content_id=summary_row_values[0],
        booking_link_id=summary_row_values[1],
        tid=summary_row_values[2],
        source_url=summary_row_values[3],
        paid_revenue_cents=summary_row_values[4],
        paid_invoice_count=summary_row_values[5],
        paid_booking_count=summary_row_values[6],
        first_paid_at=summary_row_values[7],
        last_paid_at=summary_row_values[8],
    )

    evidence_rows = db.execute(
        _creator_paid_attribution_evidence_query(
            creator_id=creator_id,
            tid=tid,
            start_date=start_date,
            end_date=end_date,
        )
    ).all()

    evidence: list[PaidAttributionEvidence] = []
    seen_invoice_ids: set[UUID] = set()
    for (
        booking_id,
        booking_uuid,
        booked_at,
        invoice_id,
        stripe_invoice_id,
        invoice_amount_cents,
        invoice_currency,
        invoice_paid_at,
        payment_event_id,
        stripe_event_id,
        payment_event_status,
        payment_event_paid_at,
        payment_event_received_at,
    ) in evidence_rows:
        if invoice_id in seen_invoice_ids:
            continue
        seen_invoice_ids.add(invoice_id)
        evidence.append(
            PaidAttributionEvidence(
                booking_id=booking_id,
                booking_uuid=booking_uuid,
                booked_at=booked_at,
                invoice_id=invoice_id,
                stripe_invoice_id=stripe_invoice_id,
                invoice_amount_cents=invoice_amount_cents,
                invoice_currency=invoice_currency,
                invoice_paid_at=invoice_paid_at,
                payment_event_id=payment_event_id,
                stripe_event_id=stripe_event_id,
                payment_event_status=payment_event_status,
                payment_event_paid_at=payment_event_paid_at,
                payment_event_received_at=payment_event_received_at,
            )
        )

    return CreatorPaidAttributionExplanation(
        creator_id=creator_id,
        start_date=start_date,
        end_date=end_date,
        summary_row=summary_row,
        evidence=evidence,
    )


def build_reports_summary_csv(summary: CreatorReportsSummary) -> str:
    output = StringIO()
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(
        [
            "content_id",
            "booking_link_id",
            "tid",
            "source_url",
            "paid_revenue_cents",
            "paid_invoice_count",
            "paid_booking_count",
            "first_paid_at",
            "last_paid_at",
        ]
    )
    for row in summary.rows:
        writer.writerow(
            [
                str(row.content_id),
                str(row.booking_link_id),
                row.tid,
                row.source_url,
                row.paid_revenue_cents,
                row.paid_invoice_count,
                row.paid_booking_count,
                _as_utc_isoformat(row.first_paid_at),
                _as_utc_isoformat(row.last_paid_at),
            ]
        )
    return output.getvalue()


def _creator_paid_summary_query(
    *,
    creator_id: UUID,
    start_date: date | None,
    end_date: date | None,
    tid: str | None = None,
):
    revenue_sum = func.coalesce(func.sum(Invoice.amount_cents), 0)
    first_paid_at = func.min(Invoice.paid_at)
    last_paid_at = func.max(Invoice.paid_at)
    filters = [
        Invoice.creator_id == creator_id,
        Invoice.status == "paid",
        Invoice.paid_at.is_not(None),
        Booking.creator_id == creator_id,
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
            revenue_sum,
            func.count(Invoice.id),
            func.count(distinct(Booking.id)),
            first_paid_at,
            last_paid_at,
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
            *filters,
        )
        .group_by(Content.id, Content.booking_link_id, Booking.tid, Content.source_url)
        .order_by(revenue_sum.desc(), last_paid_at.desc(), Booking.tid.asc())
    )


def _creator_paid_attribution_evidence_query(
    *,
    creator_id: UUID,
    tid: str,
    start_date: date | None,
    end_date: date | None,
):
    return (
        select(
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
        .where(
            Invoice.creator_id == creator_id,
            Invoice.status == "paid",
            Invoice.paid_at.is_not(None),
            Booking.creator_id == creator_id,
            Booking.tid == tid,
            *_paid_date_filters(start_date=start_date, end_date=end_date),
        )
        .order_by(
            Invoice.paid_at.desc(),
            Booking.booked_at.desc(),
            InvoicePaymentEvent.received_at.desc().nullslast(),
        )
    )


def _creator_unattributed_backlog_query(*, creator_id: UUID):
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


def _as_utc_isoformat(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _validate_date_range(*, start_date: date | None, end_date: date | None) -> None:
    if start_date is not None and end_date is not None and start_date > end_date:
        raise ValueError("start_date must be on or before end_date")
