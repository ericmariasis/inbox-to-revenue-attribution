import os
import uuid
from datetime import date, datetime, timedelta, timezone

from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.main import app
from app.models.auth_user import AuthUser
from app.models.blocked_billing_case import BlockedBillingCase
from app.models.booking import Booking
from app.models.booking_link import BookingLink
from app.models.content import Content
from app.models.creator import Creator
from app.models.invoice import Invoice
from app.models.invoice_payment_event import InvoicePaymentEvent
from app.services.blocked_billing import BLOCKED_BILLING_REASON_CREATOR_NOT_BILLABLE
from app.services.invoice_payment_events import (
    PAYMENT_PROVENANCE_CONFLICT_STATUS_NONE,
    PAYMENT_PROVENANCE_CONFLICT_STATUS_UNMATCHED_PROVIDER_SIGNAL,
    PAYMENT_PROVENANCE_STATE_CONFLICTING,
    PAYMENT_PROVENANCE_STATE_MATCHED,
    PAYMENT_PROVENANCE_STATE_PENDING,
    PAYMENT_PROVENANCE_STATUS_MATCHED,
    PAYMENT_PROVENANCE_STATUS_PENDING,
    UNATTRIBUTED_REASON_MISSING_TID,
    UNATTRIBUTED_REASON_UNKNOWN_BOOKING_UUID,
    UNATTRIBUTED_REASON_UNKNOWN_STRIPE_INVOICE_ID,
)
from app.services.reporting import (
    CURRENT_UNATTRIBUTED_BACKLOG_SCOPE,
    REPORTS_FUNNEL_STATUS_BLOCKED,
    REPORTS_FUNNEL_STATUS_NO_BOOKINGS,
    REPORTS_FUNNEL_STATUS_PAID,
    REPORTS_FUNNEL_STATUS_WAITING_FOR_PAID,
    get_creator_reports_content_drilldown,
    get_creator_paid_attribution_explanation,
    get_creator_reports_summary,
)


def _engine():
    return create_engine(os.environ["TEST_DATABASE_URL"])


def _access_token(*, user_id: str, creator_id: str, email: str, expires_delta: timedelta) -> str:
    settings = get_settings()
    issued_at = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "creator_id": creator_id,
        "email": email,
        "iat": issued_at,
        "exp": issued_at + expires_delta,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def _create_creator_with_user(
    session: Session,
    *,
    suffix: str,
    stripe_account_id: str,
) -> tuple[Creator, AuthUser]:
    creator = Creator(
        name=f"Reports Creator {suffix}",
        stripe_connect_status="connected",
        stripe_account_id=stripe_account_id,
    )
    session.add(creator)
    session.flush()

    user = AuthUser(
        creator_id=creator.id,
        email=f"reports_{suffix}@example.com",
    )
    session.add(user)
    session.flush()
    return creator, user


def _create_booking_link(
    session: Session,
    *,
    creator: Creator,
    suffix: str,
) -> BookingLink:
    booking_link = BookingLink(
        creator_id=creator.id,
        name=f"Reports Link {suffix}",
        calendly_url=f"https://calendly.com/example/reports-{suffix}",
        billing_amount_cents=19500,
        billing_currency="USD",
    )
    session.add(booking_link)
    session.flush()
    return booking_link


def _create_content(
    session: Session,
    *,
    creator: Creator,
    booking_link: BookingLink,
    suffix: str,
) -> Content:
    content = Content(
        creator_id=creator.id,
        booking_link_id=booking_link.id,
        source_url=f"https://example.com/posts/{suffix}",
        tid=f"reports_tid_{suffix}",
    )
    session.add(content)
    session.flush()
    return content


def _create_booking(
    session: Session,
    *,
    creator: Creator,
    booking_link: BookingLink,
    content: Content,
    booking_uuid: str,
    booked_at: datetime,
) -> Booking:
    booking = Booking(
        creator_id=creator.id,
        booking_link_id=booking_link.id,
        tid=content.tid,
        calendly_booking_uuid=booking_uuid,
        email=f"{booking_uuid.lower()}@example.com",
        status="created",
        booked_at=booked_at,
    )
    session.add(booking)
    session.flush()
    return booking


def _create_paid_invoice(
    session: Session,
    *,
    creator: Creator,
    booking: Booking,
    stripe_invoice_id: str,
    amount_cents: int,
    paid_at: datetime,
) -> Invoice:
    invoice = Invoice(
        creator_id=creator.id,
        booking_id=booking.id,
        tid=booking.tid,
        stripe_account_id=creator.stripe_account_id,
        stripe_invoice_id=stripe_invoice_id,
        amount_cents=amount_cents,
        currency="USD",
        status="paid",
        issued_at=paid_at - timedelta(hours=1),
        paid_at=paid_at,
    )
    session.add(invoice)
    session.flush()
    return invoice


def _create_unmatched_payment_event(
    session: Session,
    *,
    creator: Creator,
    stripe_event_id: str,
    stripe_invoice_id: str,
    reason: str,
    paid_at: datetime,
    booking: Booking | None = None,
    tid: str | None = None,
) -> InvoicePaymentEvent:
    event = InvoicePaymentEvent(
        stripe_event_id=stripe_event_id,
        stripe_event_type="invoice.paid",
        stripe_account_id=creator.stripe_account_id,
        stripe_invoice_id=stripe_invoice_id,
        invoice_id=None,
        creator_id=creator.id,
        booking_id=booking.id if booking is not None else None,
        tid=tid if tid is not None else (booking.tid if booking is not None else None),
        status="unmatched",
        unattributed_reason=reason,
        paid_at=paid_at,
        received_at=paid_at,
        processed_at=None,
    )
    session.add(event)
    session.flush()
    return event


def _create_matched_payment_event(
    session: Session,
    *,
    creator: Creator,
    booking: Booking,
    invoice: Invoice,
    stripe_event_id: str,
    paid_at: datetime,
    status: str = "applied",
) -> InvoicePaymentEvent:
    event = InvoicePaymentEvent(
        stripe_event_id=stripe_event_id,
        stripe_event_type="invoice.paid",
        stripe_account_id=creator.stripe_account_id,
        stripe_invoice_id=invoice.stripe_invoice_id,
        invoice_id=invoice.id,
        creator_id=creator.id,
        booking_id=booking.id,
        tid=booking.tid,
        status=status,
        unattributed_reason=None,
        paid_at=paid_at,
        received_at=paid_at,
        processed_at=paid_at,
    )
    session.add(event)
    session.flush()
    return event


def _create_blocked_billing_case(
    session: Session,
    *,
    creator: Creator,
    booking: Booking,
    reason_code: str,
    blocked_at: datetime,
) -> BlockedBillingCase:
    blocked_case = BlockedBillingCase(
        creator_id=creator.id,
        booking_id=booking.id,
        invoice_id=None,
        tid=booking.tid,
        calendly_booking_uuid=booking.calendly_booking_uuid,
        stripe_account_id=creator.stripe_account_id,
        frozen_amount_cents=19500,
        frozen_currency="USD",
        status="open",
        reason_code=reason_code,
        provider_operation=None,
        provider_http_status=None,
        provider_error_code=None,
        first_blocked_at=blocked_at,
        last_blocked_at=blocked_at,
        last_retry_at=None,
        resolved_at=None,
        resolution_code=None,
    )
    session.add(blocked_case)
    session.flush()
    return blocked_case


def test_creator_reports_summary_builds_content_funnel_rows_and_keeps_current_unattributed_backlog():
    engine = _engine()

    with Session(engine) as session:
        creator, _ = _create_creator_with_user(
            session,
            suffix="service",
            stripe_account_id="acct_reports_service",
        )
        booking_link = _create_booking_link(session, creator=creator, suffix="service")

        content_old = _create_content(
            session,
            creator=creator,
            booking_link=booking_link,
            suffix="service_old",
        )
        booking_old = _create_booking(
            session,
            creator=creator,
            booking_link=booking_link,
            content=content_old,
            booking_uuid="BOOK_REPORTS_SERVICE_OLD",
            booked_at=datetime(2026, 3, 7, 12, 0, tzinfo=timezone.utc),
        )
        _create_paid_invoice(
            session,
            creator=creator,
            booking=booking_old,
            stripe_invoice_id="in_reports_service_old",
            amount_cents=10000,
            paid_at=datetime(2026, 3, 7, 13, 0, tzinfo=timezone.utc),
        )

        content_current = _create_content(
            session,
            creator=creator,
            booking_link=booking_link,
            suffix="service_current",
        )
        booking_current_a = _create_booking(
            session,
            creator=creator,
            booking_link=booking_link,
            content=content_current,
            booking_uuid="BOOK_REPORTS_SERVICE_CURRENT_A",
            booked_at=datetime(2026, 3, 8, 9, 0, tzinfo=timezone.utc),
        )
        _create_paid_invoice(
            session,
            creator=creator,
            booking=booking_current_a,
            stripe_invoice_id="in_reports_service_current_a",
            amount_cents=25000,
            paid_at=datetime(2026, 3, 8, 10, 0, tzinfo=timezone.utc),
        )
        booking_current_b = _create_booking(
            session,
            creator=creator,
            booking_link=booking_link,
            content=content_current,
            booking_uuid="BOOK_REPORTS_SERVICE_CURRENT_B",
            booked_at=datetime(2026, 3, 8, 18, 0, tzinfo=timezone.utc),
        )
        _create_paid_invoice(
            session,
            creator=creator,
            booking=booking_current_b,
            stripe_invoice_id="in_reports_service_current_b",
            amount_cents=5000,
            paid_at=datetime(2026, 3, 8, 19, 0, tzinfo=timezone.utc),
        )

        _create_unmatched_payment_event(
            session,
            creator=creator,
            stripe_event_id="evt_reports_service_missing_tid",
            stripe_invoice_id="in_reports_service_missing_tid",
            reason=UNATTRIBUTED_REASON_MISSING_TID,
            paid_at=datetime(2026, 3, 8, 20, 0, tzinfo=timezone.utc),
        )
        _create_unmatched_payment_event(
            session,
            creator=creator,
            stripe_event_id="evt_reports_service_unknown_booking",
            stripe_invoice_id="in_reports_service_unknown_booking",
            reason=UNATTRIBUTED_REASON_UNKNOWN_BOOKING_UUID,
            paid_at=datetime(2026, 3, 8, 21, 0, tzinfo=timezone.utc),
        )

        blocked_content = _create_content(
            session,
            creator=creator,
            booking_link=booking_link,
            suffix="service_blocked",
        )
        blocked_booking = _create_booking(
            session,
            creator=creator,
            booking_link=booking_link,
            content=blocked_content,
            booking_uuid="BOOK_REPORTS_SERVICE_BLOCKED",
            booked_at=datetime(2026, 3, 8, 22, 0, tzinfo=timezone.utc),
        )
        _create_blocked_billing_case(
            session,
            creator=creator,
            booking=blocked_booking,
            reason_code=BLOCKED_BILLING_REASON_CREATOR_NOT_BILLABLE,
            blocked_at=datetime(2026, 3, 8, 22, 5, tzinfo=timezone.utc),
        )
        waiting_content = _create_content(
            session,
            creator=creator,
            booking_link=booking_link,
            suffix="service_waiting",
        )
        _create_booking(
            session,
            creator=creator,
            booking_link=booking_link,
            content=waiting_content,
            booking_uuid="BOOK_REPORTS_SERVICE_WAITING",
            booked_at=datetime(2026, 3, 8, 23, 0, tzinfo=timezone.utc),
        )
        empty_content = _create_content(
            session,
            creator=creator,
            booking_link=booking_link,
            suffix="service_empty",
        )

        other_creator, _ = _create_creator_with_user(
            session,
            suffix="service_other",
            stripe_account_id="acct_reports_service_other",
        )
        other_booking_link = _create_booking_link(
            session,
            creator=other_creator,
            suffix="service_other",
        )
        other_content = _create_content(
            session,
            creator=other_creator,
            booking_link=other_booking_link,
            suffix="service_other",
        )
        other_booking = _create_booking(
            session,
            creator=other_creator,
            booking_link=other_booking_link,
            content=other_content,
            booking_uuid="BOOK_REPORTS_SERVICE_OTHER",
            booked_at=datetime(2026, 3, 8, 11, 0, tzinfo=timezone.utc),
        )
        _create_paid_invoice(
            session,
            creator=other_creator,
            booking=other_booking,
            stripe_invoice_id="in_reports_service_other",
            amount_cents=99999,
            paid_at=datetime(2026, 3, 8, 12, 0, tzinfo=timezone.utc),
        )

        creator_id = creator.id
        old_content_id = str(content_old.id)
        current_content_id = str(content_current.id)
        blocked_content_id = str(blocked_content.id)
        waiting_content_id = str(waiting_content.id)
        empty_content_id = str(empty_content.id)
        booking_link_id = str(booking_link.id)
        old_tid = content_old.tid
        current_tid = content_current.tid
        blocked_tid = blocked_content.tid
        waiting_tid = waiting_content.tid
        empty_tid = empty_content.tid
        session.commit()

    with Session(engine) as session:
        full_summary = get_creator_reports_summary(
            creator_id=creator_id,
            db=session,
        )
        filtered_summary = get_creator_reports_summary(
            creator_id=creator_id,
            db=session,
            start_date=date(2026, 3, 8),
            end_date=date(2026, 3, 8),
        )

    assert [
        (
            row.content_id,
            row.tid,
            row.booking_count,
            row.paid_revenue_cents,
            row.open_blocked_billing_case_count,
            row.funnel_status,
        )
        for row in full_summary.rows
    ] == [
        (uuid.UUID(current_content_id), current_tid, 2, 30000, 0, REPORTS_FUNNEL_STATUS_PAID),
        (uuid.UUID(old_content_id), old_tid, 1, 10000, 0, REPORTS_FUNNEL_STATUS_PAID),
        (
            uuid.UUID(blocked_content_id),
            blocked_tid,
            1,
            0,
            1,
            REPORTS_FUNNEL_STATUS_BLOCKED,
        ),
        (
            uuid.UUID(waiting_content_id),
            waiting_tid,
            1,
            0,
            0,
            REPORTS_FUNNEL_STATUS_WAITING_FOR_PAID,
        ),
        (
            uuid.UUID(empty_content_id),
            empty_tid,
            0,
            0,
            0,
            REPORTS_FUNNEL_STATUS_NO_BOOKINGS,
        ),
    ]
    assert full_summary.paid_revenue_cents == 40000
    assert full_summary.paid_invoice_count == 3
    assert full_summary.paid_booking_count == 3
    assert full_summary.rows[0].booking_link_id == uuid.UUID(booking_link_id)
    assert full_summary.rows[0].booking_count == 2
    assert full_summary.rows[0].paid_invoice_count == 2
    assert full_summary.rows[0].paid_booking_count == 2
    assert full_summary.rows[0].first_paid_at == datetime(2026, 3, 8, 10, 0, tzinfo=timezone.utc)
    assert full_summary.rows[0].last_paid_at == datetime(2026, 3, 8, 19, 0, tzinfo=timezone.utc)
    assert full_summary.rows[2].first_paid_at is None
    assert full_summary.rows[2].last_paid_at is None

    assert filtered_summary.start_date == date(2026, 3, 8)
    assert filtered_summary.end_date == date(2026, 3, 8)
    assert [
        (
            row.content_id,
            row.tid,
            row.booking_count,
            row.paid_revenue_cents,
            row.funnel_status,
        )
        for row in filtered_summary.rows
    ] == [
        (uuid.UUID(current_content_id), current_tid, 2, 30000, REPORTS_FUNNEL_STATUS_PAID),
    ]
    assert filtered_summary.paid_revenue_cents == 30000
    assert filtered_summary.paid_invoice_count == 2
    assert filtered_summary.paid_booking_count == 2
    assert filtered_summary.unattributed_current_backlog.scope == CURRENT_UNATTRIBUTED_BACKLOG_SCOPE
    assert filtered_summary.unattributed_current_backlog.event_count == 2
    assert [
        (item.reason, item.event_count)
        for item in filtered_summary.unattributed_current_backlog.reasons
    ] == [
        (UNATTRIBUTED_REASON_MISSING_TID, 1),
        (UNATTRIBUTED_REASON_UNKNOWN_BOOKING_UUID, 1),
    ]
    assert filtered_summary.blocked_summary.supported is True
    assert filtered_summary.blocked_summary.reason is None
    assert filtered_summary.blocked_summary.open_case_count == 1
    assert [
        (item.reason_code, item.case_count)
        for item in filtered_summary.blocked_summary.reasons
    ] == [(BLOCKED_BILLING_REASON_CREATOR_NOT_BILLABLE, 1)]


def test_creator_reports_content_drilldown_returns_bookings_paid_and_content_scoped_diagnostics():
    engine = _engine()

    with Session(engine) as session:
        creator, _ = _create_creator_with_user(
            session,
            suffix="drilldown",
            stripe_account_id="acct_reports_drilldown",
        )
        booking_link = _create_booking_link(session, creator=creator, suffix="drilldown")
        content = _create_content(
            session,
            creator=creator,
            booking_link=booking_link,
            suffix="drilldown",
        )
        paid_booking = _create_booking(
            session,
            creator=creator,
            booking_link=booking_link,
            content=content,
            booking_uuid="BOOK_REPORTS_DRILLDOWN_PAID",
            booked_at=datetime(2026, 3, 8, 8, 0, tzinfo=timezone.utc),
        )
        invoice = _create_paid_invoice(
            session,
            creator=creator,
            booking=paid_booking,
            stripe_invoice_id="in_reports_drilldown_paid",
            amount_cents=19500,
            paid_at=datetime(2026, 3, 8, 9, 0, tzinfo=timezone.utc),
        )
        _create_matched_payment_event(
            session,
            creator=creator,
            booking=paid_booking,
            invoice=invoice,
            stripe_event_id="evt_reports_drilldown_paid",
            paid_at=invoice.paid_at,
        )

        waiting_booking = _create_booking(
            session,
            creator=creator,
            booking_link=booking_link,
            content=content,
            booking_uuid="BOOK_REPORTS_DRILLDOWN_WAITING",
            booked_at=datetime(2026, 3, 8, 10, 0, tzinfo=timezone.utc),
        )
        blocked_booking = _create_booking(
            session,
            creator=creator,
            booking_link=booking_link,
            content=content,
            booking_uuid="BOOK_REPORTS_DRILLDOWN_BLOCKED",
            booked_at=datetime(2026, 3, 8, 11, 0, tzinfo=timezone.utc),
        )
        _create_blocked_billing_case(
            session,
            creator=creator,
            booking=blocked_booking,
            reason_code=BLOCKED_BILLING_REASON_CREATOR_NOT_BILLABLE,
            blocked_at=datetime(2026, 3, 8, 11, 5, tzinfo=timezone.utc),
        )
        scoped_unmatched = _create_unmatched_payment_event(
            session,
            creator=creator,
            stripe_event_id="evt_reports_drilldown_scoped_unmatched",
            stripe_invoice_id="in_reports_drilldown_scoped_unmatched",
            reason=UNATTRIBUTED_REASON_UNKNOWN_STRIPE_INVOICE_ID,
            paid_at=datetime(2026, 3, 8, 11, 30, tzinfo=timezone.utc),
            booking=waiting_booking,
        )
        scoped_unmatched_id = scoped_unmatched.id
        _create_unmatched_payment_event(
            session,
            creator=creator,
            stripe_event_id="evt_reports_drilldown_global_unmatched",
            stripe_invoice_id="in_reports_drilldown_global_unmatched",
            reason=UNATTRIBUTED_REASON_MISSING_TID,
            paid_at=datetime(2026, 3, 8, 11, 45, tzinfo=timezone.utc),
        )

        other_creator, _ = _create_creator_with_user(
            session,
            suffix="drilldown_other",
            stripe_account_id="acct_reports_drilldown_other",
        )
        other_booking_link = _create_booking_link(
            session,
            creator=other_creator,
            suffix="drilldown_other",
        )
        other_content = _create_content(
            session,
            creator=other_creator,
            booking_link=other_booking_link,
            suffix="drilldown_other",
        )
        other_booking = _create_booking(
            session,
            creator=other_creator,
            booking_link=other_booking_link,
            content=other_content,
            booking_uuid="BOOK_REPORTS_DRILLDOWN_OTHER",
            booked_at=datetime(2026, 3, 8, 12, 0, tzinfo=timezone.utc),
        )
        _create_paid_invoice(
            session,
            creator=other_creator,
            booking=other_booking,
            stripe_invoice_id="in_reports_drilldown_other",
            amount_cents=88000,
            paid_at=datetime(2026, 3, 8, 13, 0, tzinfo=timezone.utc),
        )

        creator_id = creator.id
        other_creator_id = other_creator.id
        content_tid = content.tid
        session.commit()

    with Session(engine) as session:
        drilldown = get_creator_reports_content_drilldown(
            creator_id=creator_id,
            tid=content_tid,
            db=session,
            start_date=date(2026, 3, 8),
            end_date=date(2026, 3, 8),
        )
        hidden_from_other_creator = get_creator_reports_content_drilldown(
            creator_id=other_creator_id,
            tid=content_tid,
            db=session,
        )

    assert drilldown is not None
    assert drilldown.booking_link_name == "Reports Link drilldown"
    assert drilldown.current_summary_row.tid == content_tid
    assert drilldown.current_summary_row.booking_count == 3
    assert drilldown.current_summary_row.paid_revenue_cents == 19500
    assert drilldown.current_summary_row.open_blocked_billing_case_count == 1
    assert drilldown.current_summary_row.funnel_status == REPORTS_FUNNEL_STATUS_PAID
    assert drilldown.paid_window.paid_revenue_cents == 19500
    assert drilldown.paid_window.paid_invoice_count == 1
    assert drilldown.paid_window.paid_booking_count == 1
    assert drilldown.paid_window.first_paid_at == datetime(2026, 3, 8, 9, 0, tzinfo=timezone.utc)
    assert drilldown.paid_window.last_paid_at == datetime(2026, 3, 8, 9, 0, tzinfo=timezone.utc)
    assert [booking.provider_booking_id for booking in drilldown.bookings] == [
        "BOOK_REPORTS_DRILLDOWN_BLOCKED",
        "BOOK_REPORTS_DRILLDOWN_WAITING",
        "BOOK_REPORTS_DRILLDOWN_PAID",
    ]
    assert len(drilldown.blocked_cases) == 1
    assert drilldown.blocked_cases[0].reason_code == BLOCKED_BILLING_REASON_CREATOR_NOT_BILLABLE
    assert len(drilldown.unmatched_payment_events) == 1
    assert drilldown.unmatched_payment_events[0].payment_event_id == scoped_unmatched_id
    assert drilldown.unmatched_payment_events[0].tid == content_tid
    assert drilldown.paid_explanation is not None
    assert drilldown.paid_explanation.summary_row.tid == content_tid
    assert len(drilldown.paid_explanation.evidence) == 1
    assert hidden_from_other_creator is None


def test_creator_paid_attribution_explanation_returns_canonical_chain_for_creator_scoped_row():
    engine = _engine()

    with Session(engine) as session:
        creator, _ = _create_creator_with_user(
            session,
            suffix="explanation",
            stripe_account_id="acct_reports_explanation",
        )
        other_creator, _ = _create_creator_with_user(
            session,
            suffix="explanation_other",
            stripe_account_id="acct_reports_explanation_other",
        )
        booking_link = _create_booking_link(
            session,
            creator=creator,
            suffix="explanation",
        )
        content = _create_content(
            session,
            creator=creator,
            booking_link=booking_link,
            suffix="explanation",
        )
        booking = _create_booking(
            session,
            creator=creator,
            booking_link=booking_link,
            content=content,
            booking_uuid="BOOK_REPORTS_EXPLANATION",
            booked_at=datetime(2026, 3, 8, 8, 0, tzinfo=timezone.utc),
        )
        invoice = _create_paid_invoice(
            session,
            creator=creator,
            booking=booking,
            stripe_invoice_id="in_reports_explanation",
            amount_cents=19500,
            paid_at=datetime(2026, 3, 8, 9, 0, tzinfo=timezone.utc),
        )
        _create_matched_payment_event(
            session,
            creator=creator,
            booking=booking,
            invoice=invoice,
            stripe_event_id="evt_reports_explanation",
            paid_at=datetime(2026, 3, 8, 9, 0, tzinfo=timezone.utc),
        )

        creator_id = creator.id
        other_creator_id = other_creator.id
        content_tid = content.tid
        content_source_url = content.source_url
        booking_link_id = booking_link.id
        session.commit()

    with Session(engine) as session:
        explanation = get_creator_paid_attribution_explanation(
            creator_id=creator_id,
            tid=content_tid,
            db=session,
            start_date=date(2026, 3, 8),
            end_date=date(2026, 3, 8),
        )
        hidden_from_other_creator = get_creator_paid_attribution_explanation(
            creator_id=other_creator_id,
            tid=content_tid,
            db=session,
        )
        filtered_out = get_creator_paid_attribution_explanation(
            creator_id=creator_id,
            tid=content_tid,
            db=session,
            start_date=date(2026, 3, 9),
            end_date=date(2026, 3, 9),
        )

    assert explanation is not None
    assert explanation.summary_row.booking_link_id == booking_link_id
    assert explanation.summary_row.tid == content_tid
    assert explanation.summary_row.source_url == content_source_url
    assert explanation.summary_row.paid_revenue_cents == 19500
    assert explanation.summary_row.paid_invoice_count == 1
    assert explanation.summary_row.paid_booking_count == 1
    assert len(explanation.evidence) == 1
    assert explanation.evidence[0].booking_uuid == "BOOK_REPORTS_EXPLANATION"
    assert explanation.evidence[0].stripe_invoice_id == "in_reports_explanation"
    assert explanation.evidence[0].stripe_event_id == "evt_reports_explanation"
    assert explanation.evidence[0].payment_event_status == "applied"
    assert explanation.evidence[0].payment_provenance.status == PAYMENT_PROVENANCE_STATUS_MATCHED
    assert (
        explanation.evidence[0].payment_provenance.conflict_status
        == PAYMENT_PROVENANCE_CONFLICT_STATUS_NONE
    )
    assert explanation.evidence[0].payment_provenance.conflict_event_count == 0
    assert explanation.evidence[0].payment_provenance.conflict_reasons == ()
    assert explanation.evidence[0].payment_provenance.state == PAYMENT_PROVENANCE_STATE_MATCHED
    assert hidden_from_other_creator is None
    assert filtered_out is None


def test_creator_paid_attribution_explanation_keeps_settled_invoice_without_payment_event():
    engine = _engine()

    with Session(engine) as session:
        creator, _ = _create_creator_with_user(
            session,
            suffix="explanation_no_event",
            stripe_account_id="acct_reports_explanation_no_event",
        )
        booking_link = _create_booking_link(
            session,
            creator=creator,
            suffix="explanation_no_event",
        )
        content = _create_content(
            session,
            creator=creator,
            booking_link=booking_link,
            suffix="explanation_no_event",
        )
        booking = _create_booking(
            session,
            creator=creator,
            booking_link=booking_link,
            content=content,
            booking_uuid="BOOK_REPORTS_EXPLANATION_NO_EVENT",
            booked_at=datetime(2026, 3, 8, 10, 0, tzinfo=timezone.utc),
        )
        _create_paid_invoice(
            session,
            creator=creator,
            booking=booking,
            stripe_invoice_id="in_reports_explanation_no_event",
            amount_cents=19500,
            paid_at=datetime(2026, 3, 8, 11, 0, tzinfo=timezone.utc),
        )

        creator_id = creator.id
        content_tid = content.tid
        session.commit()

    with Session(engine) as session:
        explanation = get_creator_paid_attribution_explanation(
            creator_id=creator_id,
            tid=content_tid,
            db=session,
            start_date=date(2026, 3, 8),
            end_date=date(2026, 3, 8),
        )

    assert explanation is not None
    assert explanation.summary_row.tid == content_tid
    assert explanation.summary_row.paid_revenue_cents == 19500
    assert explanation.summary_row.paid_invoice_count == 1
    assert len(explanation.evidence) == 1
    assert explanation.evidence[0].stripe_invoice_id == "in_reports_explanation_no_event"
    assert explanation.evidence[0].stripe_event_id is None
    assert explanation.evidence[0].payment_event_status is None
    assert explanation.evidence[0].payment_provenance.status == PAYMENT_PROVENANCE_STATUS_PENDING
    assert (
        explanation.evidence[0].payment_provenance.conflict_status
        == PAYMENT_PROVENANCE_CONFLICT_STATUS_NONE
    )
    assert explanation.evidence[0].payment_provenance.conflict_event_count == 0
    assert explanation.evidence[0].payment_provenance.conflict_reasons == ()
    assert explanation.evidence[0].payment_provenance.state == PAYMENT_PROVENANCE_STATE_PENDING


def test_creator_paid_attribution_explanation_keeps_settled_invoice_when_provider_conflict_is_diagnostic():
    engine = _engine()

    with Session(engine) as session:
        creator, _ = _create_creator_with_user(
            session,
            suffix="explanation_conflict",
            stripe_account_id="acct_reports_explanation_conflict",
        )
        booking_link = _create_booking_link(
            session,
            creator=creator,
            suffix="explanation_conflict",
        )
        content = _create_content(
            session,
            creator=creator,
            booking_link=booking_link,
            suffix="explanation_conflict",
        )
        booking = _create_booking(
            session,
            creator=creator,
            booking_link=booking_link,
            content=content,
            booking_uuid="BOOK_REPORTS_EXPLANATION_CONFLICT",
            booked_at=datetime(2026, 3, 8, 12, 0, tzinfo=timezone.utc),
        )
        invoice = _create_paid_invoice(
            session,
            creator=creator,
            booking=booking,
            stripe_invoice_id="in_reports_explanation_conflict",
            amount_cents=19500,
            paid_at=datetime(2026, 3, 8, 13, 0, tzinfo=timezone.utc),
        )
        _create_matched_payment_event(
            session,
            creator=creator,
            booking=booking,
            invoice=invoice,
            stripe_event_id="evt_reports_explanation_conflict_applied",
            paid_at=invoice.paid_at,
        )
        _create_unmatched_payment_event(
            session,
            creator=creator,
            stripe_event_id="evt_reports_explanation_conflict_unmatched",
            stripe_invoice_id=invoice.stripe_invoice_id,
            reason=UNATTRIBUTED_REASON_UNKNOWN_STRIPE_INVOICE_ID,
            paid_at=invoice.paid_at,
        )

        creator_id = creator.id
        content_tid = content.tid
        session.commit()

    with Session(engine) as session:
        explanation = get_creator_paid_attribution_explanation(
            creator_id=creator_id,
            tid=content_tid,
            db=session,
            start_date=date(2026, 3, 8),
            end_date=date(2026, 3, 8),
        )

    assert explanation is not None
    assert explanation.summary_row.tid == content_tid
    assert explanation.summary_row.paid_revenue_cents == 19500
    assert explanation.summary_row.paid_invoice_count == 1
    assert len(explanation.evidence) == 1
    assert explanation.evidence[0].stripe_invoice_id == "in_reports_explanation_conflict"
    assert explanation.evidence[0].stripe_event_id == "evt_reports_explanation_conflict_applied"
    assert explanation.evidence[0].payment_event_status == "applied"
    assert explanation.evidence[0].payment_provenance.status == PAYMENT_PROVENANCE_STATUS_MATCHED
    assert (
        explanation.evidence[0].payment_provenance.conflict_status
        == PAYMENT_PROVENANCE_CONFLICT_STATUS_UNMATCHED_PROVIDER_SIGNAL
    )
    assert explanation.evidence[0].payment_provenance.conflict_event_count == 1
    assert explanation.evidence[0].payment_provenance.conflict_reasons == (
        UNATTRIBUTED_REASON_UNKNOWN_STRIPE_INVOICE_ID,
    )
    assert explanation.evidence[0].payment_provenance.state == PAYMENT_PROVENANCE_STATE_CONFLICTING


def test_creator_paid_attribution_explanation_surfaces_paypal_provider_identity():
    engine = _engine()

    with Session(engine) as session:
        creator = Creator(
            name="Reports PayPal Creator",
            billing_provider="paypal",
            billing_connect_status="connected",
            billing_account_id="merchant_reports_paypal",
        )
        session.add(creator)
        session.flush()

        booking_link = _create_booking_link(
            session,
            creator=creator,
            suffix="paypal",
        )
        content = _create_content(
            session,
            creator=creator,
            booking_link=booking_link,
            suffix="paypal",
        )
        booking = _create_booking(
            session,
            creator=creator,
            booking_link=booking_link,
            content=content,
            booking_uuid="BOOK_REPORTS_PAYPAL",
            booked_at=datetime(2026, 3, 9, 8, 0, tzinfo=timezone.utc),
        )
        invoice = Invoice(
            creator_id=creator.id,
            booking_id=booking.id,
            tid=booking.tid,
            payment_provider="paypal",
            provider_account_id="merchant_reports_paypal",
            provider_invoice_id="INV_REPORTS_PAYPAL",
            amount_cents=19500,
            currency="USD",
            status="paid",
            issued_at=datetime(2026, 3, 9, 8, 30, tzinfo=timezone.utc),
            paid_at=datetime(2026, 3, 9, 9, 0, tzinfo=timezone.utc),
        )
        session.add(invoice)
        session.flush()
        session.add(
            InvoicePaymentEvent(
                payment_provider="paypal",
                provider_event_id="WH_REPORTS_PAYPAL",
                provider_event_type="INVOICING.INVOICE.PAID",
                provider_account_id="merchant_reports_paypal",
                provider_invoice_id="INV_REPORTS_PAYPAL",
                invoice_id=invoice.id,
                creator_id=creator.id,
                booking_id=booking.id,
                tid=booking.tid,
                status="applied",
                paid_at=invoice.paid_at,
                received_at=invoice.paid_at,
                processed_at=invoice.paid_at,
            )
        )
        creator_id = creator.id
        content_tid = content.tid
        session.commit()

    with Session(engine) as session:
        explanation = get_creator_paid_attribution_explanation(
            creator_id=creator_id,
            tid=content_tid,
            db=session,
            start_date=date(2026, 3, 9),
            end_date=date(2026, 3, 9),
        )

    assert explanation is not None
    assert len(explanation.evidence) == 1
    assert explanation.evidence[0].payment_provider == "paypal"
    assert explanation.evidence[0].provider_invoice_id == "INV_REPORTS_PAYPAL"
    assert explanation.evidence[0].provider_event_id == "WH_REPORTS_PAYPAL"
    assert explanation.evidence[0].stripe_invoice_id is None
    assert explanation.evidence[0].stripe_event_id is None


def test_reports_summary_requires_auth():
    with TestClient(app) as client:
        response = client.get("/reports/summary")

    assert response.status_code == 401
    assert response.json() == {"detail": "not authenticated"}


def test_reports_summary_returns_creator_scoped_filtered_rows_and_current_unattributed_backlog():
    engine = _engine()

    with Session(engine) as session:
        creator, user = _create_creator_with_user(
            session,
            suffix="api",
            stripe_account_id="acct_reports_api",
        )
        booking_link = _create_booking_link(session, creator=creator, suffix="api")

        content_in_range = _create_content(
            session,
            creator=creator,
            booking_link=booking_link,
            suffix="api_in_range",
        )
        booking_in_range = _create_booking(
            session,
            creator=creator,
            booking_link=booking_link,
            content=content_in_range,
            booking_uuid="BOOK_REPORTS_API_IN_RANGE",
            booked_at=datetime(2026, 3, 8, 8, 0, tzinfo=timezone.utc),
        )
        _create_paid_invoice(
            session,
            creator=creator,
            booking=booking_in_range,
            stripe_invoice_id="in_reports_api_in_range",
            amount_cents=19500,
            paid_at=datetime(2026, 3, 8, 9, 0, tzinfo=timezone.utc),
        )

        content_out_of_range = _create_content(
            session,
            creator=creator,
            booking_link=booking_link,
            suffix="api_out_of_range",
        )
        booking_out_of_range = _create_booking(
            session,
            creator=creator,
            booking_link=booking_link,
            content=content_out_of_range,
            booking_uuid="BOOK_REPORTS_API_OUT_OF_RANGE",
            booked_at=datetime(2026, 3, 7, 8, 0, tzinfo=timezone.utc),
        )
        _create_paid_invoice(
            session,
            creator=creator,
            booking=booking_out_of_range,
            stripe_invoice_id="in_reports_api_out_of_range",
            amount_cents=5000,
            paid_at=datetime(2026, 3, 7, 9, 0, tzinfo=timezone.utc),
        )

        _create_unmatched_payment_event(
            session,
            creator=creator,
            stripe_event_id="evt_reports_api_missing_tid",
            stripe_invoice_id="in_reports_api_missing_tid",
            reason=UNATTRIBUTED_REASON_MISSING_TID,
            paid_at=datetime(2026, 3, 8, 11, 0, tzinfo=timezone.utc),
        )

        blocked_content = _create_content(
            session,
            creator=creator,
            booking_link=booking_link,
            suffix="api_blocked",
        )
        blocked_booking = _create_booking(
            session,
            creator=creator,
            booking_link=booking_link,
            content=blocked_content,
            booking_uuid="BOOK_REPORTS_API_BLOCKED",
            booked_at=datetime(2026, 3, 8, 11, 30, tzinfo=timezone.utc),
        )
        _create_blocked_billing_case(
            session,
            creator=creator,
            booking=blocked_booking,
            reason_code=BLOCKED_BILLING_REASON_CREATOR_NOT_BILLABLE,
            blocked_at=datetime(2026, 3, 8, 11, 35, tzinfo=timezone.utc),
        )

        other_creator, _ = _create_creator_with_user(
            session,
            suffix="api_other",
            stripe_account_id="acct_reports_api_other",
        )
        other_booking_link = _create_booking_link(
            session,
            creator=other_creator,
            suffix="api_other",
        )
        other_content = _create_content(
            session,
            creator=other_creator,
            booking_link=other_booking_link,
            suffix="api_other",
        )
        other_booking = _create_booking(
            session,
            creator=other_creator,
            booking_link=other_booking_link,
            content=other_content,
            booking_uuid="BOOK_REPORTS_API_OTHER",
            booked_at=datetime(2026, 3, 8, 14, 0, tzinfo=timezone.utc),
        )
        _create_paid_invoice(
            session,
            creator=other_creator,
            booking=other_booking,
            stripe_invoice_id="in_reports_api_other",
            amount_cents=88000,
            paid_at=datetime(2026, 3, 8, 15, 0, tzinfo=timezone.utc),
        )

        token = _access_token(
            user_id=str(user.id),
            creator_id=str(creator.id),
            email=user.email,
            expires_delta=timedelta(hours=24),
        )
        content_in_range_id = str(content_in_range.id)
        content_in_range_booking_link_id = str(booking_link.id)
        content_in_range_tid = content_in_range.tid
        content_in_range_source_url = content_in_range.source_url
        session.commit()

    with TestClient(app) as client:
        response = client.get(
            "/reports/summary",
            params={"start_date": "2026-03-08", "end_date": "2026-03-08"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    assert response.headers.get("X-Request-Id")
    assert response.json() == {
        "start_date": "2026-03-08",
        "end_date": "2026-03-08",
            "rows": [
                {
                    "content_id": content_in_range_id,
                    "booking_link_id": content_in_range_booking_link_id,
                    "tid": content_in_range_tid,
                    "source_url": content_in_range_source_url,
                    "booking_count": 1,
                    "paid_revenue_cents": 19500,
                    "paid_invoice_count": 1,
                    "paid_booking_count": 1,
                    "open_blocked_billing_case_count": 0,
                    "funnel_status": REPORTS_FUNNEL_STATUS_PAID,
                    "first_paid_at": "2026-03-08T09:00:00Z",
                    "last_paid_at": "2026-03-08T09:00:00Z",
                }
            ],
        "paid_revenue_cents": 19500,
        "paid_invoice_count": 1,
        "paid_booking_count": 1,
        "unattributed_current_backlog": {
            "scope": CURRENT_UNATTRIBUTED_BACKLOG_SCOPE,
            "event_count": 1,
            "reasons": [
                {
                    "reason": UNATTRIBUTED_REASON_MISSING_TID,
                    "event_count": 1,
                }
            ],
        },
        "blocked_summary": {
            "supported": True,
            "reason": None,
            "open_case_count": 1,
            "reasons": [
                {
                    "reason_code": BLOCKED_BILLING_REASON_CREATOR_NOT_BILLABLE,
                    "case_count": 1,
                }
            ],
        },
    }


def test_reports_summary_rejects_inverted_date_range():
    engine = _engine()

    with Session(engine) as session:
        creator, user = _create_creator_with_user(
            session,
            suffix="invalid_range",
            stripe_account_id="acct_reports_invalid_range",
        )
        token = _access_token(
            user_id=str(user.id),
            creator_id=str(creator.id),
            email=user.email,
            expires_delta=timedelta(hours=24),
        )
        session.commit()

    with TestClient(app) as client:
        response = client.get(
            "/reports/summary",
            params={"start_date": "2026-03-09", "end_date": "2026-03-08"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 422
    assert response.json() == {"detail": "start_date must be on or before end_date"}
