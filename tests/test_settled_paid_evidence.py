import os
from datetime import date, datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models.blocked_billing_case import BlockedBillingCase
from app.models.booking import Booking
from app.models.booking_link import BookingLink
from app.models.content import Content
from app.models.creator import Creator
from app.models.invoice import Invoice
from app.models.invoice_payment_event import InvoicePaymentEvent
from app.services.booking_attribution import (
    BOOKING_ATTRIBUTION_STATUS_UNATTRIBUTED,
    BOOKING_UNATTRIBUTED_REASON_MISSING_TID,
)
from app.services.invoice_payment_events import (
    PAYMENT_PROVENANCE_CONFLICT_STATUS_NONE,
    PAYMENT_PROVENANCE_CONFLICT_STATUS_UNMATCHED_PROVIDER_SIGNAL,
    PAYMENT_PROVENANCE_STATE_CONFLICTING,
    PAYMENT_PROVENANCE_STATE_MATCHED,
    PAYMENT_PROVENANCE_STATE_PENDING,
    PAYMENT_PROVENANCE_STATE_UNMATCHED,
    PAYMENT_PROVENANCE_STATUS_MATCHED,
    PAYMENT_PROVENANCE_STATUS_PENDING,
    UNATTRIBUTED_REASON_MISSING_TID,
    UNATTRIBUTED_REASON_UNKNOWN_BOOKING_UUID,
    UNATTRIBUTED_REASON_UNKNOWN_STRIPE_INVOICE_ID,
)
from app.services.settled_paid_evidence import get_creator_settled_paid_evidence


def _engine():
    return create_engine(os.environ["TEST_DATABASE_URL"])


def test_creator_settled_paid_evidence_keeps_paid_invoices_settled_and_surfaces_current_diagnostics():
    engine = _engine()

    with Session(engine) as session:
        creator = Creator(
            name="Settled Evidence Creator",
            stripe_connect_status="connected",
            stripe_account_id="acct_settled_evidence",
        )
        session.add(creator)
        session.flush()

        booking_link = BookingLink(
            creator_id=creator.id,
            name="Settled Evidence Link",
            calendly_url="https://calendly.com/example/settled-evidence-link",
            billing_amount_cents=19500,
            billing_currency="USD",
        )
        session.add(booking_link)
        session.flush()

        no_event_content = Content(
            creator_id=creator.id,
            booking_link_id=booking_link.id,
            source_url="https://example.com/posts/settled-no-event",
            tid="settled_no_event_tid",
        )
        session.add(no_event_content)
        session.flush()

        no_event_booking = Booking(
            creator_id=creator.id,
            booking_link_id=booking_link.id,
            tid=no_event_content.tid,
            calendly_booking_uuid="BOOK_SETTLED_NO_EVENT",
            email="settled-no-event@example.com",
            status="created",
            booked_at=datetime(2026, 3, 8, 8, 0, tzinfo=timezone.utc),
        )
        session.add(no_event_booking)
        session.flush()

        no_event_invoice = Invoice(
            creator_id=creator.id,
            booking_id=no_event_booking.id,
            tid=no_event_booking.tid,
            stripe_account_id=creator.stripe_account_id,
            stripe_invoice_id="in_settled_no_event",
            amount_cents=19500,
            currency="USD",
            status="paid",
            issued_at=datetime(2026, 3, 8, 8, 30, tzinfo=timezone.utc),
            paid_at=datetime(2026, 3, 8, 9, 0, tzinfo=timezone.utc),
        )
        session.add(no_event_invoice)
        session.flush()

        matched_content = Content(
            creator_id=creator.id,
            booking_link_id=booking_link.id,
            source_url="https://example.com/posts/settled-with-event",
            tid="settled_with_event_tid",
        )
        session.add(matched_content)
        session.flush()

        matched_booking = Booking(
            creator_id=creator.id,
            booking_link_id=booking_link.id,
            tid=matched_content.tid,
            calendly_booking_uuid="BOOK_SETTLED_WITH_EVENT",
            email="settled-with-event@example.com",
            status="created",
            booked_at=datetime(2026, 3, 8, 10, 0, tzinfo=timezone.utc),
        )
        session.add(matched_booking)
        session.flush()

        matched_invoice = Invoice(
            creator_id=creator.id,
            booking_id=matched_booking.id,
            tid=matched_booking.tid,
            stripe_account_id=creator.stripe_account_id,
            stripe_invoice_id="in_settled_with_event",
            amount_cents=25000,
            currency="USD",
            status="paid",
            issued_at=datetime(2026, 3, 8, 10, 30, tzinfo=timezone.utc),
            paid_at=datetime(2026, 3, 8, 11, 0, tzinfo=timezone.utc),
        )
        session.add(matched_invoice)
        session.flush()

        session.add(
            InvoicePaymentEvent(
                stripe_event_id="evt_settled_with_event",
                stripe_event_type="invoice.paid",
                stripe_account_id=creator.stripe_account_id,
                stripe_invoice_id=matched_invoice.stripe_invoice_id,
                invoice_id=matched_invoice.id,
                creator_id=creator.id,
                booking_id=matched_booking.id,
                tid=matched_booking.tid,
                status="applied",
                paid_at=matched_invoice.paid_at,
                received_at=matched_invoice.paid_at,
                processed_at=matched_invoice.paid_at,
            )
        )

        pending_conflict_content = Content(
            creator_id=creator.id,
            booking_link_id=booking_link.id,
            source_url="https://example.com/posts/settled-pending-conflict",
            tid="settled_pending_conflict_tid",
        )
        session.add(pending_conflict_content)
        session.flush()

        pending_conflict_booking = Booking(
            creator_id=creator.id,
            booking_link_id=booking_link.id,
            tid=pending_conflict_content.tid,
            calendly_booking_uuid="BOOK_SETTLED_PENDING_CONFLICT",
            email="settled-pending-conflict@example.com",
            status="created",
            booked_at=datetime(2026, 3, 8, 11, 30, tzinfo=timezone.utc),
        )
        session.add(pending_conflict_booking)
        session.flush()

        pending_conflict_invoice = Invoice(
            creator_id=creator.id,
            booking_id=pending_conflict_booking.id,
            tid=pending_conflict_booking.tid,
            stripe_account_id=creator.stripe_account_id,
            stripe_invoice_id="in_settled_pending_conflict",
            amount_cents=22500,
            currency="USD",
            status="paid",
            issued_at=datetime(2026, 3, 8, 11, 45, tzinfo=timezone.utc),
            paid_at=datetime(2026, 3, 8, 12, 0, tzinfo=timezone.utc),
        )
        session.add(pending_conflict_invoice)
        session.flush()

        session.add(
            InvoicePaymentEvent(
                stripe_event_id="evt_settled_pending_conflict_unmatched",
                stripe_event_type="invoice.paid",
                stripe_account_id=creator.stripe_account_id,
                stripe_invoice_id=pending_conflict_invoice.stripe_invoice_id,
                invoice_id=None,
                creator_id=creator.id,
                booking_id=None,
                tid=None,
                status="unmatched",
                unattributed_reason=UNATTRIBUTED_REASON_UNKNOWN_STRIPE_INVOICE_ID,
                paid_at=pending_conflict_invoice.paid_at,
                received_at=pending_conflict_invoice.paid_at,
                processed_at=None,
            )
        )

        matched_conflict_content = Content(
            creator_id=creator.id,
            booking_link_id=booking_link.id,
            source_url="https://example.com/posts/settled-matched-conflict",
            tid="settled_matched_conflict_tid",
        )
        session.add(matched_conflict_content)
        session.flush()

        matched_conflict_booking = Booking(
            creator_id=creator.id,
            booking_link_id=booking_link.id,
            tid=matched_conflict_content.tid,
            calendly_booking_uuid="BOOK_SETTLED_MATCHED_CONFLICT",
            email="settled-matched-conflict@example.com",
            status="created",
            booked_at=datetime(2026, 3, 8, 12, 15, tzinfo=timezone.utc),
        )
        session.add(matched_conflict_booking)
        session.flush()

        matched_conflict_invoice = Invoice(
            creator_id=creator.id,
            booking_id=matched_conflict_booking.id,
            tid=matched_conflict_booking.tid,
            stripe_account_id=creator.stripe_account_id,
            stripe_invoice_id="in_settled_matched_conflict",
            amount_cents=27500,
            currency="USD",
            status="paid",
            issued_at=datetime(2026, 3, 8, 12, 30, tzinfo=timezone.utc),
            paid_at=datetime(2026, 3, 8, 12, 45, tzinfo=timezone.utc),
        )
        session.add(matched_conflict_invoice)
        session.flush()

        session.add(
            InvoicePaymentEvent(
                stripe_event_id="evt_settled_matched_conflict_applied",
                stripe_event_type="invoice.paid",
                stripe_account_id=creator.stripe_account_id,
                stripe_invoice_id=matched_conflict_invoice.stripe_invoice_id,
                invoice_id=matched_conflict_invoice.id,
                creator_id=creator.id,
                booking_id=matched_conflict_booking.id,
                tid=matched_conflict_booking.tid,
                status="applied",
                paid_at=matched_conflict_invoice.paid_at,
                received_at=matched_conflict_invoice.paid_at,
                processed_at=matched_conflict_invoice.paid_at,
            )
        )
        session.add(
            InvoicePaymentEvent(
                stripe_event_id="evt_settled_matched_conflict_unmatched",
                stripe_event_type="invoice.paid",
                stripe_account_id=creator.stripe_account_id,
                stripe_invoice_id=matched_conflict_invoice.stripe_invoice_id,
                invoice_id=None,
                creator_id=creator.id,
                booking_id=None,
                tid=None,
                status="unmatched",
                unattributed_reason=UNATTRIBUTED_REASON_UNKNOWN_BOOKING_UUID,
                paid_at=matched_conflict_invoice.paid_at,
                received_at=matched_conflict_invoice.paid_at,
                processed_at=None,
            )
        )

        blocked_content = Content(
            creator_id=creator.id,
            booking_link_id=booking_link.id,
            source_url="https://example.com/posts/blocked-diagnostic",
            tid="blocked_diagnostic_tid",
        )
        session.add(blocked_content)
        session.flush()

        blocked_booking = Booking(
            creator_id=creator.id,
            booking_link_id=booking_link.id,
            tid=blocked_content.tid,
            calendly_booking_uuid="BOOK_BLOCKED_DIAGNOSTIC",
            email="blocked-diagnostic@example.com",
            status="created",
            booked_at=datetime(2026, 3, 8, 12, 0, tzinfo=timezone.utc),
        )
        session.add(blocked_booking)
        session.flush()

        session.add(
            Booking(
                creator_id=creator.id,
                booking_link_id=booking_link.id,
                tid=None,
                calendly_booking_uuid="BOOK_UNATTRIBUTED_DIAGNOSTIC",
                email="unattributed-diagnostic@example.com",
                status="created",
                attribution_status=BOOKING_ATTRIBUTION_STATUS_UNATTRIBUTED,
                unattributed_reason=BOOKING_UNATTRIBUTED_REASON_MISSING_TID,
                booked_at=datetime(2026, 3, 8, 12, 30, tzinfo=timezone.utc),
            )
        )

        session.add(
            BlockedBillingCase(
                creator_id=creator.id,
                booking_id=blocked_booking.id,
                invoice_id=None,
                tid=blocked_booking.tid,
                calendly_booking_uuid=blocked_booking.calendly_booking_uuid,
                stripe_account_id=creator.stripe_account_id,
                frozen_amount_cents=19500,
                frozen_currency="USD",
                status="open",
                reason_code="creator_not_billable",
                provider_operation=None,
                provider_http_status=None,
                provider_error_code=None,
                first_blocked_at=datetime(2026, 3, 8, 12, 5, tzinfo=timezone.utc),
                last_blocked_at=datetime(2026, 3, 8, 12, 5, tzinfo=timezone.utc),
                last_retry_at=None,
                resolved_at=None,
                resolution_code=None,
            )
        )

        session.add(
            InvoicePaymentEvent(
                stripe_event_id="evt_settled_unmatched",
                stripe_event_type="invoice.paid",
                stripe_account_id=creator.stripe_account_id,
                stripe_invoice_id="in_settled_unmatched",
                invoice_id=None,
                creator_id=creator.id,
                booking_id=None,
                tid=None,
                status="unmatched",
                unattributed_reason=UNATTRIBUTED_REASON_MISSING_TID,
                paid_at=datetime(2026, 3, 8, 13, 0, tzinfo=timezone.utc),
                received_at=datetime(2026, 3, 8, 13, 0, tzinfo=timezone.utc),
                processed_at=None,
            )
        )

        other_creator = Creator(
            name="Settled Evidence Other Creator",
            stripe_connect_status="connected",
            stripe_account_id="acct_settled_evidence_other",
        )
        session.add(other_creator)
        session.flush()

        other_booking_link = BookingLink(
            creator_id=other_creator.id,
            name="Settled Evidence Other Link",
            calendly_url="https://calendly.com/example/settled-evidence-other-link",
            billing_amount_cents=9900,
            billing_currency="USD",
        )
        session.add(other_booking_link)
        session.flush()

        other_content = Content(
            creator_id=other_creator.id,
            booking_link_id=other_booking_link.id,
            source_url="https://example.com/posts/settled-other",
            tid="settled_other_tid",
        )
        session.add(other_content)
        session.flush()

        other_booking = Booking(
            creator_id=other_creator.id,
            booking_link_id=other_booking_link.id,
            tid=other_content.tid,
            calendly_booking_uuid="BOOK_SETTLED_OTHER",
            email="settled-other@example.com",
            status="created",
            booked_at=datetime(2026, 3, 8, 14, 0, tzinfo=timezone.utc),
        )
        session.add(other_booking)
        session.flush()

        session.add(
            Invoice(
                creator_id=other_creator.id,
                booking_id=other_booking.id,
                tid=other_booking.tid,
                stripe_account_id=other_creator.stripe_account_id,
                stripe_invoice_id="in_settled_other",
                amount_cents=88000,
                currency="USD",
                status="paid",
                issued_at=datetime(2026, 3, 8, 14, 30, tzinfo=timezone.utc),
                paid_at=datetime(2026, 3, 8, 15, 0, tzinfo=timezone.utc),
            )
        )

        creator_id = creator.id
        session.commit()

    with Session(engine) as session:
        snapshot = get_creator_settled_paid_evidence(
            creator_id=creator_id,
            db=session,
            start_date=date(2026, 3, 8),
            end_date=date(2026, 3, 8),
        )

    assert sorted(row.stripe_invoice_id for row in snapshot.settled_rows) == [
        "in_settled_matched_conflict",
        "in_settled_no_event",
        "in_settled_pending_conflict",
        "in_settled_with_event",
    ]

    no_event_row = next(
        row for row in snapshot.settled_rows if row.stripe_invoice_id == "in_settled_no_event"
    )
    assert no_event_row.payment_event_id is None
    assert no_event_row.stripe_event_id is None
    assert no_event_row.payment_event_status is None
    assert no_event_row.payment_provenance.status == PAYMENT_PROVENANCE_STATUS_PENDING
    assert no_event_row.payment_provenance.conflict_status == PAYMENT_PROVENANCE_CONFLICT_STATUS_NONE
    assert no_event_row.payment_provenance.conflict_event_count == 0
    assert no_event_row.payment_provenance.conflict_reasons == ()
    assert no_event_row.payment_provenance.state == PAYMENT_PROVENANCE_STATE_PENDING

    matched_row = next(
        row for row in snapshot.settled_rows if row.stripe_invoice_id == "in_settled_with_event"
    )
    assert matched_row.stripe_event_id == "evt_settled_with_event"
    assert matched_row.payment_event_status == "applied"
    assert matched_row.payment_provenance.status == PAYMENT_PROVENANCE_STATUS_MATCHED
    assert matched_row.payment_provenance.conflict_status == PAYMENT_PROVENANCE_CONFLICT_STATUS_NONE
    assert matched_row.payment_provenance.conflict_event_count == 0
    assert matched_row.payment_provenance.conflict_reasons == ()
    assert matched_row.payment_provenance.state == PAYMENT_PROVENANCE_STATE_MATCHED

    pending_conflict_row = next(
        row
        for row in snapshot.settled_rows
        if row.stripe_invoice_id == "in_settled_pending_conflict"
    )
    assert pending_conflict_row.payment_event_id is None
    assert pending_conflict_row.stripe_event_id is None
    assert pending_conflict_row.payment_event_status is None
    assert pending_conflict_row.payment_provenance.status == PAYMENT_PROVENANCE_STATUS_PENDING
    assert (
        pending_conflict_row.payment_provenance.conflict_status
        == PAYMENT_PROVENANCE_CONFLICT_STATUS_UNMATCHED_PROVIDER_SIGNAL
    )
    assert pending_conflict_row.payment_provenance.conflict_event_count == 1
    assert pending_conflict_row.payment_provenance.conflict_reasons == (
        UNATTRIBUTED_REASON_UNKNOWN_STRIPE_INVOICE_ID,
    )
    assert pending_conflict_row.payment_provenance.state == PAYMENT_PROVENANCE_STATE_UNMATCHED

    matched_conflict_row = next(
        row
        for row in snapshot.settled_rows
        if row.stripe_invoice_id == "in_settled_matched_conflict"
    )
    assert matched_conflict_row.stripe_event_id == "evt_settled_matched_conflict_applied"
    assert matched_conflict_row.payment_event_status == "applied"
    assert matched_conflict_row.payment_provenance.status == PAYMENT_PROVENANCE_STATUS_MATCHED
    assert (
        matched_conflict_row.payment_provenance.conflict_status
        == PAYMENT_PROVENANCE_CONFLICT_STATUS_UNMATCHED_PROVIDER_SIGNAL
    )
    assert matched_conflict_row.payment_provenance.conflict_event_count == 1
    assert matched_conflict_row.payment_provenance.conflict_reasons == (
        UNATTRIBUTED_REASON_UNKNOWN_BOOKING_UUID,
    )
    assert matched_conflict_row.payment_provenance.state == PAYMENT_PROVENANCE_STATE_CONFLICTING

    assert snapshot.unmatched_payment_backlog.event_count == 3
    assert [
        (item.reason, item.event_count)
        for item in snapshot.unmatched_payment_backlog.reasons
    ] == [
        (UNATTRIBUTED_REASON_MISSING_TID, 1),
        (UNATTRIBUTED_REASON_UNKNOWN_BOOKING_UUID, 1),
        (UNATTRIBUTED_REASON_UNKNOWN_STRIPE_INVOICE_ID, 1),
    ]

    assert snapshot.blocked_billing_backlog.open_case_count == 1
    assert [
        (item.reason_code, item.case_count)
        for item in snapshot.blocked_billing_backlog.reasons
    ] == [("creator_not_billable", 1)]


def test_creator_settled_paid_evidence_surfaces_paypal_provider_identity():
    engine = _engine()

    with Session(engine) as session:
        creator = Creator(
            name="Settled Evidence PayPal Creator",
            billing_provider="paypal",
            billing_connect_status="connected",
            billing_account_id="merchant_settled_paypal",
        )
        session.add(creator)
        session.flush()

        booking_link = BookingLink(
            creator_id=creator.id,
            name="Settled Evidence PayPal Link",
            calendly_url="https://calendly.com/example/settled-evidence-paypal-link",
            billing_amount_cents=19500,
            billing_currency="USD",
        )
        session.add(booking_link)
        session.flush()

        content = Content(
            creator_id=creator.id,
            booking_link_id=booking_link.id,
            source_url="https://example.com/posts/settled-paypal",
            tid="settled_paypal_tid",
        )
        session.add(content)
        session.flush()

        booking = Booking(
            creator_id=creator.id,
            booking_link_id=booking_link.id,
            tid=content.tid,
            calendly_booking_uuid="BOOK_SETTLED_PAYPAL",
            email="settled-paypal@example.com",
            status="created",
            booked_at=datetime(2026, 3, 9, 8, 0, tzinfo=timezone.utc),
        )
        session.add(booking)
        session.flush()

        invoice = Invoice(
            creator_id=creator.id,
            booking_id=booking.id,
            tid=booking.tid,
            payment_provider="paypal",
            provider_account_id="merchant_settled_paypal",
            provider_invoice_id="INV_SETTLED_PAYPAL",
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
                provider_event_id="WH_SETTLED_PAYPAL",
                provider_event_type="INVOICING.INVOICE.PAID",
                provider_account_id="merchant_settled_paypal",
                provider_invoice_id="INV_SETTLED_PAYPAL",
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
        session.commit()

    with Session(engine) as session:
        snapshot = get_creator_settled_paid_evidence(
            creator_id=creator_id,
            db=session,
            start_date=date(2026, 3, 9),
            end_date=date(2026, 3, 9),
        )

    assert len(snapshot.settled_rows) == 1
    row = snapshot.settled_rows[0]
    assert row.payment_provider == "paypal"
    assert row.provider_invoice_id == "INV_SETTLED_PAYPAL"
    assert row.provider_event_id == "WH_SETTLED_PAYPAL"
    assert row.stripe_invoice_id is None
    assert row.stripe_event_id is None
