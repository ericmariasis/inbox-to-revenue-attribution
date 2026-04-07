import csv
from dataclasses import dataclass
from datetime import UTC, date, datetime
from io import StringIO
from uuid import UUID

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.models.blocked_billing_case import BlockedBillingCase
from app.models.booking import Booking
from app.models.billing_provider import BILLING_PROVIDER_STRIPE
from app.models.booking_link import BookingLink
from app.models.content import Content
from app.models.content_confirmed_topic import ContentConfirmedTopic
from app.models.content_topic_candidate import ContentTopicCandidate
from app.services.blocked_billing import (
    BlockedBillingCaseSummary,
    list_open_blocked_billing_cases,
)
from app.services.content_topics import (
    CONTENT_TOPIC_REVIEW_STATUS_CONFIRMED,
    normalize_topic_label_display,
)
from app.services.booking_attribution import BOOKING_ATTRIBUTION_STATUS_ATTRIBUTED
from app.services.invoice_payment_events import (
    PaymentProvenanceSummary,
    UnmatchedPaymentEventSummary,
    list_current_unmatched_payment_events,
)
from app.services.settled_paid_evidence import (
    CURRENT_UNMATCHED_PAYMENT_BACKLOG_SCOPE,
    SettledPaidEvidenceRow,
    get_creator_settled_paid_evidence,
)

CURRENT_UNATTRIBUTED_BACKLOG_SCOPE = CURRENT_UNMATCHED_PAYMENT_BACKLOG_SCOPE
REPORTS_FUNNEL_STATUS_BLOCKED = "blocked_before_invoicing"
REPORTS_FUNNEL_STATUS_NO_BOOKINGS = "no_bookings_yet"
REPORTS_FUNNEL_STATUS_PAID = "paid_result_recorded"
REPORTS_FUNNEL_STATUS_WAITING_FOR_PAID = "waiting_for_first_paid_result"


@dataclass(frozen=True)
class ReportsSummaryRow:
    content_id: UUID
    booking_link_id: UUID
    tid: str
    source_url: str
    booking_count: int
    paid_revenue_cents: int
    paid_invoice_count: int
    paid_booking_count: int
    open_blocked_billing_case_count: int
    funnel_status: str
    first_paid_at: datetime | None
    last_paid_at: datetime | None


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
class CreatorPaidAttributionExplanation:
    creator_id: UUID
    start_date: date | None
    end_date: date | None
    summary_row: ReportsSummaryRow
    evidence: list[PaidAttributionEvidence]


@dataclass(frozen=True)
class ReportsContentBooking:
    booking_id: UUID
    provider_booking_id: str
    booking_link_id: UUID
    booking_link_name: str
    status: str
    booked_at: datetime
    canceled_at: datetime | None


@dataclass(frozen=True)
class ReportsPaidWindowSummary:
    paid_revenue_cents: int
    paid_invoice_count: int
    paid_booking_count: int
    first_paid_at: datetime | None
    last_paid_at: datetime | None


@dataclass(frozen=True)
class CreatorReportsContentDrilldown:
    creator_id: UUID
    start_date: date | None
    end_date: date | None
    booking_link_name: str
    current_summary_row: ReportsSummaryRow
    paid_window: ReportsPaidWindowSummary
    bookings: list[ReportsContentBooking]
    blocked_cases: list[BlockedBillingCaseSummary]
    unmatched_payment_events: list[UnmatchedPaymentEventSummary]
    paid_explanation: CreatorPaidAttributionExplanation | None


@dataclass(frozen=True)
class ReportsTopicSummaryRow:
    canonical_label: str
    normalized_label: str
    content_count: int
    booking_count: int
    paid_revenue_cents: int
    paid_invoice_count: int
    paid_booking_count: int
    open_blocked_billing_case_count: int
    funnel_status: str
    first_paid_at: datetime | None
    last_paid_at: datetime | None


@dataclass(frozen=True)
class CreatorReportsTopicSummary:
    creator_id: UUID
    start_date: date | None
    end_date: date | None
    rows: list[ReportsTopicSummaryRow]
    has_any_authoritative_topics: bool


@dataclass(frozen=True)
class _PaidReportsSummaryMetrics:
    paid_revenue_cents: int = 0
    paid_invoice_count: int = 0
    paid_booking_count: int = 0
    first_paid_at: datetime | None = None
    last_paid_at: datetime | None = None


@dataclass(frozen=True)
class _AuthoritativeReportsTopic:
    canonical_label: str
    normalized_label: str


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

    rows = _build_reports_summary_rows(
        creator_id=creator_id,
        db=db,
        settled_rows=snapshot.settled_rows,
        include_unpaid_content=start_date is None and end_date is None,
    )
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

    summary_rows = _build_reports_summary_rows(
        creator_id=creator_id,
        db=db,
        settled_rows=snapshot.settled_rows,
        include_unpaid_content=False,
    )
    if not summary_rows:
        return None

    summary_row = summary_rows[0]
    evidence = [
        PaidAttributionEvidence(
            booking_id=row.booking_id,
            booking_uuid=row.booking_uuid,
            booked_at=row.booked_at,
            invoice_id=row.invoice_id,
            payment_provider=row.payment_provider,
            provider_invoice_id=row.provider_invoice_id,
            invoice_amount_cents=row.invoice_amount_cents,
            invoice_currency=row.invoice_currency,
            invoice_paid_at=row.invoice_paid_at,
            payment_event_id=row.payment_event_id,
            provider_event_id=row.provider_event_id,
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


def get_creator_reports_content_drilldown(
    *,
    creator_id: UUID,
    tid: str,
    db: Session,
    start_date: date | None = None,
    end_date: date | None = None,
) -> CreatorReportsContentDrilldown | None:
    current_summary = get_creator_reports_summary(
        creator_id=creator_id,
        db=db,
    )
    current_summary_row = next((row for row in current_summary.rows if row.tid == tid), None)
    if current_summary_row is None:
        return None

    booking_link_name = db.scalar(
        select(BookingLink.name).where(
            BookingLink.id == current_summary_row.booking_link_id,
            BookingLink.creator_id == creator_id,
        )
    )
    if booking_link_name is None:
        return None

    paid_explanation = get_creator_paid_attribution_explanation(
        creator_id=creator_id,
        tid=tid,
        db=db,
        start_date=start_date,
        end_date=end_date,
    )
    paid_window = ReportsPaidWindowSummary(
        paid_revenue_cents=(
            paid_explanation.summary_row.paid_revenue_cents if paid_explanation is not None else 0
        ),
        paid_invoice_count=(
            paid_explanation.summary_row.paid_invoice_count if paid_explanation is not None else 0
        ),
        paid_booking_count=(
            paid_explanation.summary_row.paid_booking_count if paid_explanation is not None else 0
        ),
        first_paid_at=(
            paid_explanation.summary_row.first_paid_at if paid_explanation is not None else None
        ),
        last_paid_at=(
            paid_explanation.summary_row.last_paid_at if paid_explanation is not None else None
        ),
    )

    booking_rows = db.execute(
        select(Booking, BookingLink.name)
        .select_from(Booking)
        .join(
            BookingLink,
            and_(
                BookingLink.id == Booking.booking_link_id,
                BookingLink.creator_id == creator_id,
            ),
        )
        .where(
            Booking.creator_id == creator_id,
            Booking.tid == tid,
            Booking.attribution_status == BOOKING_ATTRIBUTION_STATUS_ATTRIBUTED,
        )
        .order_by(Booking.booked_at.desc(), Booking.id.desc())
    ).all()
    bookings = [
        ReportsContentBooking(
            booking_id=booking.id,
            provider_booking_id=booking.resolved_provider_booking_id or "missing",
            booking_link_id=booking.booking_link_id,
            booking_link_name=booking_link_name_value,
            status=booking.status,
            booked_at=booking.booked_at,
            canceled_at=booking.canceled_at,
        )
        for booking, booking_link_name_value in booking_rows
    ]

    blocked_cases = [
        blocked_case
        for blocked_case in list_open_blocked_billing_cases(
            creator_id=creator_id,
            db=db,
        )
        if blocked_case.tid == tid
    ]
    unmatched_payment_events = [
        payment_event
        for payment_event in list_current_unmatched_payment_events(
            creator_id=creator_id,
            db=db,
        )
        if payment_event.tid == tid
    ]

    return CreatorReportsContentDrilldown(
        creator_id=creator_id,
        start_date=start_date,
        end_date=end_date,
        booking_link_name=booking_link_name,
        current_summary_row=current_summary_row,
        paid_window=paid_window,
        bookings=bookings,
        blocked_cases=blocked_cases,
        unmatched_payment_events=unmatched_payment_events,
        paid_explanation=paid_explanation,
    )


def get_creator_reports_topic_summary(
    *,
    creator_id: UUID,
    db: Session,
    start_date: date | None = None,
    end_date: date | None = None,
) -> CreatorReportsTopicSummary:
    content_summary = get_creator_reports_summary(
        creator_id=creator_id,
        db=db,
        start_date=start_date,
        end_date=end_date,
    )
    authoritative_topics_by_content_id = _load_creator_authoritative_topics_by_content_id(
        creator_id=creator_id,
        db=db,
    )
    rows = _build_reports_topic_summary_rows(
        content_rows=content_summary.rows,
        authoritative_topics_by_content_id=authoritative_topics_by_content_id,
    )

    return CreatorReportsTopicSummary(
        creator_id=creator_id,
        start_date=start_date,
        end_date=end_date,
        rows=rows,
        has_any_authoritative_topics=bool(authoritative_topics_by_content_id),
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
            "booking_count",
            "paid_revenue_cents",
            "paid_invoice_count",
            "paid_booking_count",
            "open_blocked_billing_case_count",
            "funnel_status",
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
                row.booking_count,
                row.paid_revenue_cents,
                row.paid_invoice_count,
                row.paid_booking_count,
                row.open_blocked_billing_case_count,
                row.funnel_status,
                _as_utc_isoformat(row.first_paid_at) if row.first_paid_at is not None else "",
                _as_utc_isoformat(row.last_paid_at) if row.last_paid_at is not None else "",
            ]
        )
    return output.getvalue()


def _build_reports_summary_rows(
    *,
    creator_id: UUID,
    db: Session,
    settled_rows: list[SettledPaidEvidenceRow],
    include_unpaid_content: bool,
) -> list[ReportsSummaryRow]:
    grouped: dict[tuple[UUID, UUID, str, str], _PaidReportsSummaryMetrics] = {}
    booking_ids_by_group: dict[tuple[UUID, UUID, str, str], set[UUID]] = {}

    for row in settled_rows:
        key = (row.content_id, row.booking_link_id, row.tid, row.source_url)
        existing = grouped.get(key)
        if existing is None:
            grouped[key] = _PaidReportsSummaryMetrics(
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
        grouped[key] = _PaidReportsSummaryMetrics(
            paid_revenue_cents=existing.paid_revenue_cents + row.invoice_amount_cents,
            paid_invoice_count=existing.paid_invoice_count + 1,
            paid_booking_count=len(booking_ids),
            first_paid_at=min(existing.first_paid_at, row.invoice_paid_at),
            last_paid_at=max(existing.last_paid_at, row.invoice_paid_at),
        )

    blocked_case_count_by_tid = {
        tid: case_count
        for tid, case_count in db.execute(
            select(
                BlockedBillingCase.tid,
                func.count(BlockedBillingCase.id),
            )
            .where(
                BlockedBillingCase.creator_id == creator_id,
                BlockedBillingCase.status == "open",
            )
            .group_by(BlockedBillingCase.tid)
        ).all()
        if tid is not None
    }

    content_rows = db.execute(
        select(
            Content.id,
            Content.booking_link_id,
            Content.tid,
            Content.source_url,
            func.count(Booking.id),
        )
        .select_from(Content)
        .outerjoin(
            Booking,
            and_(
                Booking.creator_id == creator_id,
                Booking.tid == Content.tid,
                Booking.attribution_status == BOOKING_ATTRIBUTION_STATUS_ATTRIBUTED,
            ),
        )
        .where(Content.creator_id == creator_id)
        .group_by(
            Content.id,
            Content.booking_link_id,
            Content.tid,
            Content.source_url,
        )
    ).all()

    rows: list[ReportsSummaryRow] = []
    for content_id, booking_link_id, tid, source_url, booking_count in content_rows:
        key = (content_id, booking_link_id, tid, source_url)
        paid_metrics = grouped.get(key, _PaidReportsSummaryMetrics())
        if not include_unpaid_content and paid_metrics.paid_invoice_count == 0:
            continue

        open_blocked_billing_case_count = blocked_case_count_by_tid.get(tid, 0)
        rows.append(
            ReportsSummaryRow(
                content_id=content_id,
                booking_link_id=booking_link_id,
                tid=tid,
                source_url=source_url,
                booking_count=booking_count,
                paid_revenue_cents=paid_metrics.paid_revenue_cents,
                paid_invoice_count=paid_metrics.paid_invoice_count,
                paid_booking_count=paid_metrics.paid_booking_count,
                open_blocked_billing_case_count=open_blocked_billing_case_count,
                funnel_status=_resolve_reports_funnel_status(
                    booking_count=booking_count,
                    paid_invoice_count=paid_metrics.paid_invoice_count,
                    open_blocked_billing_case_count=open_blocked_billing_case_count,
                ),
                first_paid_at=paid_metrics.first_paid_at,
                last_paid_at=paid_metrics.last_paid_at,
            )
        )

    return sorted(
        rows,
        key=lambda row: (
            -(1 if row.paid_invoice_count > 0 else 0),
            -row.paid_revenue_cents,
            -(row.last_paid_at.timestamp() if row.last_paid_at is not None else 0),
            -row.booking_count,
            -row.open_blocked_billing_case_count,
            row.tid,
        ),
    )


def _load_creator_authoritative_topics_by_content_id(
    *,
    creator_id: UUID,
    db: Session,
) -> dict[UUID, list[_AuthoritativeReportsTopic]]:
    topic_rows = db.execute(
        select(
            Content.id,
            ContentConfirmedTopic.canonical_label,
            ContentConfirmedTopic.normalized_label,
        )
        .select_from(Content)
        .join(
            ContentTopicCandidate,
            and_(
                ContentTopicCandidate.content_id == Content.id,
                ContentTopicCandidate.creator_id == creator_id,
                ContentTopicCandidate.extraction_artifact_id
                == Content.authoritative_extraction_artifact_id,
                ContentTopicCandidate.review_status
                == CONTENT_TOPIC_REVIEW_STATUS_CONFIRMED,
                ContentTopicCandidate.confirmed_topic_id.is_not(None),
            ),
        )
        .join(
            ContentConfirmedTopic,
            and_(
                ContentConfirmedTopic.id == ContentTopicCandidate.confirmed_topic_id,
                ContentConfirmedTopic.content_id == Content.id,
                ContentConfirmedTopic.creator_id == creator_id,
            ),
        )
        .where(
            Content.creator_id == creator_id,
            Content.authoritative_extraction_artifact_id.is_not(None),
        )
        .order_by(
            Content.id.asc(),
            ContentTopicCandidate.candidate_rank.asc(),
            ContentTopicCandidate.created_at.asc(),
            ContentTopicCandidate.id.asc(),
        )
    ).all()

    topics_by_content_id: dict[UUID, list[_AuthoritativeReportsTopic]] = {}
    seen_topic_labels_by_content_id: dict[UUID, set[str]] = {}
    for content_id, canonical_label, normalized_label in topic_rows:
        seen_labels = seen_topic_labels_by_content_id.setdefault(content_id, set())
        if normalized_label in seen_labels:
            continue
        seen_labels.add(normalized_label)
        topics_by_content_id.setdefault(content_id, []).append(
            _AuthoritativeReportsTopic(
                canonical_label=normalize_topic_label_display(canonical_label),
                normalized_label=normalized_label,
            )
        )

    return topics_by_content_id


def _build_reports_topic_summary_rows(
    *,
    content_rows: list[ReportsSummaryRow],
    authoritative_topics_by_content_id: dict[UUID, list[_AuthoritativeReportsTopic]],
) -> list[ReportsTopicSummaryRow]:
    grouped: dict[str, dict[str, object]] = {}

    for content_row in content_rows:
        for topic in authoritative_topics_by_content_id.get(content_row.content_id, []):
            existing = grouped.get(topic.normalized_label)
            if existing is None:
                grouped[topic.normalized_label] = {
                    "canonical_label": topic.canonical_label,
                    "normalized_label": topic.normalized_label,
                    "content_count": 1,
                    "booking_count": content_row.booking_count,
                    "paid_revenue_cents": content_row.paid_revenue_cents,
                    "paid_invoice_count": content_row.paid_invoice_count,
                    "paid_booking_count": content_row.paid_booking_count,
                    "open_blocked_billing_case_count": content_row.open_blocked_billing_case_count,
                    "first_paid_at": content_row.first_paid_at,
                    "last_paid_at": content_row.last_paid_at,
                }
                continue

            existing["content_count"] = int(existing["content_count"]) + 1
            existing["booking_count"] = int(existing["booking_count"]) + content_row.booking_count
            existing["paid_revenue_cents"] = (
                int(existing["paid_revenue_cents"]) + content_row.paid_revenue_cents
            )
            existing["paid_invoice_count"] = (
                int(existing["paid_invoice_count"]) + content_row.paid_invoice_count
            )
            existing["paid_booking_count"] = (
                int(existing["paid_booking_count"]) + content_row.paid_booking_count
            )
            existing["open_blocked_billing_case_count"] = (
                int(existing["open_blocked_billing_case_count"])
                + content_row.open_blocked_billing_case_count
            )
            if _reports_topic_label_sort_key(topic.canonical_label) < _reports_topic_label_sort_key(
                str(existing["canonical_label"])
            ):
                existing["canonical_label"] = topic.canonical_label
            if content_row.first_paid_at is not None:
                existing_first_paid_at = existing["first_paid_at"]
                if (
                    existing_first_paid_at is None
                    or content_row.first_paid_at < existing_first_paid_at
                ):
                    existing["first_paid_at"] = content_row.first_paid_at
            if content_row.last_paid_at is not None:
                existing_last_paid_at = existing["last_paid_at"]
                if (
                    existing_last_paid_at is None
                    or content_row.last_paid_at > existing_last_paid_at
                ):
                    existing["last_paid_at"] = content_row.last_paid_at

    rows = [
        ReportsTopicSummaryRow(
            canonical_label=str(group["canonical_label"]),
            normalized_label=str(group["normalized_label"]),
            content_count=int(group["content_count"]),
            booking_count=int(group["booking_count"]),
            paid_revenue_cents=int(group["paid_revenue_cents"]),
            paid_invoice_count=int(group["paid_invoice_count"]),
            paid_booking_count=int(group["paid_booking_count"]),
            open_blocked_billing_case_count=int(group["open_blocked_billing_case_count"]),
            funnel_status=_resolve_reports_funnel_status(
                booking_count=int(group["booking_count"]),
                paid_invoice_count=int(group["paid_invoice_count"]),
                open_blocked_billing_case_count=int(group["open_blocked_billing_case_count"]),
            ),
            first_paid_at=group["first_paid_at"],
            last_paid_at=group["last_paid_at"],
        )
        for group in grouped.values()
    ]

    return sorted(
        rows,
        key=lambda row: (
            -(1 if row.paid_invoice_count > 0 else 0),
            -row.paid_revenue_cents,
            -(row.last_paid_at.timestamp() if row.last_paid_at is not None else 0),
            -row.booking_count,
            -row.content_count,
            row.normalized_label,
        ),
    )


def _reports_topic_label_sort_key(label: str) -> tuple[str, str]:
    return (label.casefold(), label)


def _resolve_reports_funnel_status(
    *,
    booking_count: int,
    paid_invoice_count: int,
    open_blocked_billing_case_count: int,
) -> str:
    if paid_invoice_count > 0:
        return REPORTS_FUNNEL_STATUS_PAID
    if open_blocked_billing_case_count > 0:
        return REPORTS_FUNNEL_STATUS_BLOCKED
    if booking_count > 0:
        return REPORTS_FUNNEL_STATUS_WAITING_FOR_PAID
    return REPORTS_FUNNEL_STATUS_NO_BOOKINGS


def _as_utc_isoformat(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
