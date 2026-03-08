from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID

from sqlalchemy import and_, distinct, func, select
from sqlalchemy.orm import Session

from app.models.booking import Booking
from app.models.content import Content
from app.models.invoice import Invoice
from app.models.invoice_payment_event import InvoicePaymentEvent

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


def _creator_paid_summary_query(
    *,
    creator_id: UUID,
    start_date: date | None,
    end_date: date | None,
):
    revenue_sum = func.coalesce(func.sum(Invoice.amount_cents), 0)
    first_paid_at = func.min(Invoice.paid_at)
    last_paid_at = func.max(Invoice.paid_at)

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
            Invoice.creator_id == creator_id,
            Invoice.status == "paid",
            Invoice.paid_at.is_not(None),
            Booking.creator_id == creator_id,
            *_paid_date_filters(start_date=start_date, end_date=end_date),
        )
        .group_by(Content.id, Content.booking_link_id, Booking.tid, Content.source_url)
        .order_by(revenue_sum.desc(), last_paid_at.desc(), Booking.tid.asc())
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


def _validate_date_range(*, start_date: date | None, end_date: date | None) -> None:
    if start_date is not None and end_date is not None and start_date > end_date:
        raise ValueError("start_date must be on or before end_date")
