import csv
from dataclasses import dataclass
from datetime import UTC, date, datetime
from io import StringIO
from uuid import UUID

from sqlalchemy.orm import Session

from app.services.invoice_payment_events import PaymentProvenanceSummary
from app.services.settled_paid_evidence import (
    CURRENT_UNMATCHED_PAYMENT_BACKLOG_SCOPE,
    SettledPaidEvidenceRow,
    get_creator_settled_paid_evidence,
)

CURRENT_UNATTRIBUTED_BACKLOG_SCOPE = CURRENT_UNMATCHED_PAYMENT_BACKLOG_SCOPE


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
class ReportsBlockedReasonCount:
    reason_code: str
    case_count: int


@dataclass(frozen=True)
class ReportsBlockedSummary:
    supported: bool
    reason: str | None
    open_case_count: int
    reasons: list[ReportsBlockedReasonCount]


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
    payment_provenance: PaymentProvenanceSummary


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
    snapshot = get_creator_settled_paid_evidence(
        creator_id=creator_id,
        db=db,
        start_date=start_date,
        end_date=end_date,
    )

    rows = _build_reports_summary_rows(snapshot.settled_rows)
    unattributed_reasons = [
        ReportsUnattributedReasonCount(
            reason=item.reason,
            event_count=item.event_count,
        )
        for item in snapshot.unmatched_payment_backlog.reasons
    ]
    blocked_reasons = [
        ReportsBlockedReasonCount(
            reason_code=item.reason_code,
            case_count=item.case_count,
        )
        for item in snapshot.blocked_billing_backlog.reasons
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
            scope=snapshot.unmatched_payment_backlog.scope,
            event_count=snapshot.unmatched_payment_backlog.event_count,
            reasons=unattributed_reasons,
        ),
        blocked_summary=ReportsBlockedSummary(
            supported=True,
            reason=None,
            open_case_count=snapshot.blocked_billing_backlog.open_case_count,
            reasons=blocked_reasons,
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
    snapshot = get_creator_settled_paid_evidence(
        creator_id=creator_id,
        db=db,
        start_date=start_date,
        end_date=end_date,
        tid=tid,
    )
    if not snapshot.settled_rows:
        return None

    summary_rows = _build_reports_summary_rows(snapshot.settled_rows)
    if not summary_rows:
        return None

    summary_row = summary_rows[0]
    evidence = [
        PaidAttributionEvidence(
            booking_id=row.booking_id,
            booking_uuid=row.booking_uuid,
            booked_at=row.booked_at,
            invoice_id=row.invoice_id,
            stripe_invoice_id=row.stripe_invoice_id,
            invoice_amount_cents=row.invoice_amount_cents,
            invoice_currency=row.invoice_currency,
            invoice_paid_at=row.invoice_paid_at,
            payment_event_id=row.payment_event_id,
            stripe_event_id=row.stripe_event_id,
            payment_event_status=row.payment_event_status,
            payment_event_paid_at=row.payment_event_paid_at,
            payment_event_received_at=row.payment_event_received_at,
            payment_provenance=row.payment_provenance,
        )
        for row in snapshot.settled_rows
    ]

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


def _build_reports_summary_rows(
    settled_rows: list[SettledPaidEvidenceRow],
) -> list[ReportsSummaryRow]:
    grouped: dict[tuple[UUID, UUID, str, str], ReportsSummaryRow] = {}
    booking_ids_by_group: dict[tuple[UUID, UUID, str, str], set[UUID]] = {}

    for row in settled_rows:
        key = (row.content_id, row.booking_link_id, row.tid, row.source_url)
        existing = grouped.get(key)
        if existing is None:
            grouped[key] = ReportsSummaryRow(
                content_id=row.content_id,
                booking_link_id=row.booking_link_id,
                tid=row.tid,
                source_url=row.source_url,
                paid_revenue_cents=row.invoice_amount_cents,
                paid_invoice_count=1,
                paid_booking_count=1,
                first_paid_at=row.invoice_paid_at,
                last_paid_at=row.invoice_paid_at,
            )
            booking_ids_by_group[key] = {row.booking_id}
            continue

        booking_ids = booking_ids_by_group[key]
        booking_ids.add(row.booking_id)
        grouped[key] = ReportsSummaryRow(
            content_id=existing.content_id,
            booking_link_id=existing.booking_link_id,
            tid=existing.tid,
            source_url=existing.source_url,
            paid_revenue_cents=existing.paid_revenue_cents + row.invoice_amount_cents,
            paid_invoice_count=existing.paid_invoice_count + 1,
            paid_booking_count=len(booking_ids),
            first_paid_at=min(existing.first_paid_at, row.invoice_paid_at),
            last_paid_at=max(existing.last_paid_at, row.invoice_paid_at),
        )

    return sorted(
        grouped.values(),
        key=lambda row: (
            -row.paid_revenue_cents,
            -row.last_paid_at.timestamp(),
            row.tid,
        ),
    )


def _as_utc_isoformat(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
