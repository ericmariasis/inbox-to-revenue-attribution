import os
from datetime import datetime, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models.booking import Booking
from app.models.booking_link import BookingLink
from app.models.content import Content
from app.models.creator import Creator
from app.models.invoice import Invoice
from app.models.invoice_payment_event import InvoicePaymentEvent
from app.services.invoice_payment_events import (
    InvoicePaidEventHints,
    InvoicePaymentEventService,
    UNATTRIBUTED_REASON_MISSING_TID,
    UNATTRIBUTED_REASON_UNKNOWN_PROVIDER_INVOICE_ID,
    UNATTRIBUTED_REASON_UNKNOWN_STRIPE_INVOICE_ID,
)
from app.services.billing_lifecycle import RECONCILE_REASON_MISSING_PROVIDER_ACCOUNT_ID


def _engine():
    return create_engine(os.environ["TEST_DATABASE_URL"])


def _create_creator(session: Session, *, name: str, stripe_account_id: str) -> Creator:
    creator = Creator(
        name=name,
        stripe_connect_status="connected",
        stripe_account_id=stripe_account_id,
    )
    session.add(creator)
    session.flush()
    return creator


def _create_booking_chain(
    session: Session,
    *,
    creator: Creator,
    suffix: str,
) -> tuple[Content, Booking]:
    booking_link = BookingLink(
        creator_id=creator.id,
        name=f"Story 49 Link {suffix}",
        calendly_url=f"https://calendly.com/example/story49-{suffix}",
    )
    session.add(booking_link)
    session.flush()

    content = Content(
        creator_id=creator.id,
        booking_link_id=booking_link.id,
        source_url=f"https://example.com/story49/{suffix}",
        tid=f"story49_tid_{suffix}",
    )
    session.add(content)
    session.flush()

    booking = Booking(
        creator_id=creator.id,
        tid=content.tid,
        booking_link_id=booking_link.id,
        calendly_booking_uuid=f"BOOK_story49_{suffix}",
        email=f"story49-{suffix}@example.com",
        status="created",
        booked_at=datetime(2026, 3, 8, 23, 0, tzinfo=timezone.utc),
    )
    session.add(booking)
    session.flush()
    return content, booking


def _create_invoice(
    session: Session,
    *,
    creator: Creator,
    booking: Booking,
    tid: str,
    stripe_invoice_id: str,
    amount_cents: int,
    status: str = "open",
) -> Invoice:
    invoice = Invoice(
        creator_id=creator.id,
        booking_id=booking.id,
        tid=tid,
        stripe_account_id=creator.stripe_account_id,
        stripe_invoice_id=stripe_invoice_id,
        amount_cents=amount_cents,
        currency="USD",
        status=status,
        issued_at=datetime(2026, 3, 8, 23, 5, tzinfo=timezone.utc),
    )
    session.add(invoice)
    session.flush()
    return invoice


def test_reconcile_unmatched_payment_event_links_invoice_and_is_idempotent():
    engine = _engine()
    service = InvoicePaymentEventService(session_factory=lambda: Session(engine))
    paid_at = datetime(2026, 3, 8, 23, 30, tzinfo=timezone.utc)

    with Session(engine) as session:
        creator = _create_creator(
            session,
            name="Story 49 Reconcile Creator",
            stripe_account_id="acct_story49_reconcile",
        )
        content, booking = _create_booking_chain(session, creator=creator, suffix="reconcile")
        creator_id = creator.id
        booking_id = booking.id
        booking_uuid = booking.calendly_booking_uuid
        tid = content.tid
        session.commit()

    first_result = service.handle_invoice_paid_event(
        stripe_event_id="evt_story49_reconcile",
        stripe_event_type="invoice.paid",
        stripe_account_id="acct_story49_reconcile",
        stripe_invoice_id="in_story49_reconcile",
        paid_at=paid_at,
        received_at=datetime(2026, 3, 8, 23, 31, tzinfo=timezone.utc),
        hints=InvoicePaidEventHints(
            booking_uuid=booking_uuid,
            tid=tid,
        ),
    )

    assert first_result.outcome == "unmatched"
    assert first_result.creator_id == creator_id
    assert first_result.booking_id == booking_id
    assert first_result.tid == tid
    assert first_result.unattributed_reason == UNATTRIBUTED_REASON_UNKNOWN_STRIPE_INVOICE_ID

    with Session(engine) as session:
        creator = session.get(Creator, creator_id)
        booking = session.get(Booking, booking_id)
        assert creator is not None
        assert booking is not None
        invoice = _create_invoice(
            session,
            creator=creator,
            booking=booking,
            tid=tid,
            stripe_invoice_id="in_story49_reconcile",
            amount_cents=19500,
        )
        invoice_id = invoice.id
        session.commit()

    reconciliation = service.reconcile_unmatched_payment_event(
        stripe_event_id="evt_story49_reconcile"
    )
    second_reconciliation = service.reconcile_unmatched_payment_event(
        stripe_event_id="evt_story49_reconcile"
    )

    with Session(engine) as session:
        invoice = session.get(Invoice, invoice_id)
        payment_event = session.scalar(
            select(InvoicePaymentEvent).where(
                InvoicePaymentEvent.stripe_event_id == "evt_story49_reconcile"
            )
        )

    assert reconciliation.outcome == "reconciled"
    assert reconciliation.invoice_id == invoice_id
    assert reconciliation.creator_id == creator_id
    assert reconciliation.booking_id == booking_id
    assert reconciliation.booking_uuid == booking_uuid
    assert reconciliation.tid == tid
    assert second_reconciliation.outcome == "already_reconciled"
    assert invoice is not None
    assert invoice.status == "paid"
    assert invoice.paid_at == paid_at
    assert payment_event is not None
    assert payment_event.payment_provider == "stripe"
    assert payment_event.provider_event_id == "evt_story49_reconcile"
    assert payment_event.provider_event_type == "invoice.paid"
    assert payment_event.provider_account_id == "acct_story49_reconcile"
    assert payment_event.provider_invoice_id == "in_story49_reconcile"
    assert payment_event.invoice_id == invoice_id
    assert payment_event.creator_id == creator_id
    assert payment_event.booking_id == booking_id
    assert payment_event.tid == tid
    assert payment_event.status == "reconciled"
    assert payment_event.unattributed_reason is None
    assert payment_event.processed_at is not None


def test_provider_neutral_service_reconciles_unmatched_event_without_legacy_stripe_identity():
    engine = _engine()
    service = InvoicePaymentEventService(session_factory=lambda: Session(engine))
    paid_at = datetime(2026, 3, 8, 23, 40, tzinfo=timezone.utc)

    with Session(engine) as session:
        creator = Creator(
            name="PP-3 Neutral Event Creator",
            billing_provider="paypal",
            billing_connect_status="connected",
            billing_account_id="merchant_pp3_service",
        )
        session.add(creator)
        session.flush()
        content, booking = _create_booking_chain(session, creator=creator, suffix="pp3_paypal")
        creator_id = creator.id
        booking_id = booking.id
        booking_uuid = booking.calendly_booking_uuid
        tid = content.tid
        session.commit()

    first_result = service.handle_provider_invoice_paid_event(
        payment_provider="paypal",
        provider_event_id="WH-PP3-SERVICE",
        provider_event_type="INVOICING.INVOICE.PAID",
        provider_account_id="merchant_pp3_service",
        provider_invoice_id="INV-PP3-SERVICE",
        paid_at=paid_at,
        received_at=datetime(2026, 3, 8, 23, 41, tzinfo=timezone.utc),
        hints=InvoicePaidEventHints(
            booking_uuid=booking_uuid,
            tid=tid,
        ),
    )

    assert first_result.outcome == "unmatched"
    assert first_result.creator_id == creator_id
    assert first_result.booking_id == booking_id
    assert first_result.tid == tid
    assert first_result.unattributed_reason == UNATTRIBUTED_REASON_UNKNOWN_STRIPE_INVOICE_ID

    with Session(engine) as session:
        creator = session.get(Creator, creator_id)
        booking = session.get(Booking, booking_id)
        assert creator is not None
        assert booking is not None
        invoice = Invoice(
            creator_id=creator.id,
            booking_id=booking.id,
            tid=tid,
            payment_provider="paypal",
            provider_account_id="merchant_pp3_service",
            provider_invoice_id="INV-PP3-SERVICE",
            amount_cents=20500,
            currency="USD",
            status="open",
            issued_at=datetime(2026, 3, 8, 23, 42, tzinfo=timezone.utc),
        )
        session.add(invoice)
        session.flush()
        invoice_id = invoice.id
        session.commit()

    reconciliation = service.reconcile_provider_unmatched_payment_event(
        payment_provider="paypal",
        provider_event_id="WH-PP3-SERVICE",
    )
    second_reconciliation = service.reconcile_provider_unmatched_payment_event(
        payment_provider="paypal",
        provider_event_id="WH-PP3-SERVICE",
    )

    with Session(engine) as session:
        invoice = session.get(Invoice, invoice_id)
        payment_event = session.scalar(
            select(InvoicePaymentEvent).where(
                InvoicePaymentEvent.payment_provider == "paypal",
                InvoicePaymentEvent.provider_event_id == "WH-PP3-SERVICE",
            )
        )

    assert reconciliation.outcome == "reconciled"
    assert reconciliation.invoice_id == invoice_id
    assert reconciliation.creator_id == creator_id
    assert reconciliation.booking_id == booking_id
    assert reconciliation.booking_uuid == booking_uuid
    assert reconciliation.tid == tid
    assert second_reconciliation.outcome == "already_reconciled"
    assert invoice is not None
    assert invoice.status == "paid"
    assert invoice.paid_at == paid_at
    assert payment_event is not None
    assert payment_event.payment_provider == "paypal"
    assert payment_event.provider_event_id == "WH-PP3-SERVICE"
    assert payment_event.provider_event_type == "INVOICING.INVOICE.PAID"
    assert payment_event.provider_account_id == "merchant_pp3_service"
    assert payment_event.provider_invoice_id == "INV-PP3-SERVICE"
    assert payment_event.stripe_event_id is None
    assert payment_event.stripe_event_type is None
    assert payment_event.stripe_account_id is None
    assert payment_event.stripe_invoice_id is None
    assert payment_event.invoice_id == invoice_id
    assert payment_event.creator_id == creator_id
    assert payment_event.booking_id == booking_id
    assert payment_event.tid == tid
    assert payment_event.status == "reconciled"
    assert payment_event.unattributed_reason is None
    assert payment_event.processed_at is not None


def test_provider_neutral_service_persists_explicit_unmatched_reason_override():
    engine = _engine()
    service = InvoicePaymentEventService(session_factory=lambda: Session(engine))
    paid_at = datetime(2026, 3, 8, 23, 45, tzinfo=timezone.utc)

    result = service.handle_provider_invoice_paid_event(
        payment_provider="paypal",
        provider_event_id="WH-PP8-UNMATCHED",
        provider_event_type="INVOICING.INVOICE.PAID",
        provider_account_id=None,
        provider_invoice_id="INV-PP8-UNMATCHED",
        paid_at=paid_at,
        received_at=datetime(2026, 3, 8, 23, 46, tzinfo=timezone.utc),
        unmatched_reason_override=UNATTRIBUTED_REASON_UNKNOWN_PROVIDER_INVOICE_ID,
    )

    with Session(engine) as session:
        payment_event = session.scalar(
            select(InvoicePaymentEvent).where(
                InvoicePaymentEvent.payment_provider == "paypal",
                InvoicePaymentEvent.provider_event_id == "WH-PP8-UNMATCHED",
            )
        )

    assert result.outcome == "unmatched"
    assert result.unattributed_reason == UNATTRIBUTED_REASON_UNKNOWN_PROVIDER_INVOICE_ID
    assert payment_event is not None
    assert payment_event.status == "unmatched"
    assert payment_event.unattributed_reason == UNATTRIBUTED_REASON_UNKNOWN_PROVIDER_INVOICE_ID
    assert payment_event.provider_account_id is None
    assert payment_event.provider_invoice_id == "INV-PP8-UNMATCHED"


def test_reconcile_provider_unmatched_payment_event_stays_pending_without_provider_account_id():
    engine = _engine()
    service = InvoicePaymentEventService(session_factory=lambda: Session(engine))
    paid_at = datetime(2026, 3, 8, 23, 47, tzinfo=timezone.utc)

    result = service.handle_provider_invoice_paid_event(
        payment_provider="paypal",
        provider_event_id="WH-PP8-PENDING",
        provider_event_type="INVOICING.INVOICE.PAID",
        provider_account_id=None,
        provider_invoice_id="INV-PP8-PENDING",
        paid_at=paid_at,
        received_at=datetime(2026, 3, 8, 23, 48, tzinfo=timezone.utc),
        unmatched_reason_override=UNATTRIBUTED_REASON_UNKNOWN_PROVIDER_INVOICE_ID,
    )
    reconciliation = service.reconcile_provider_unmatched_payment_event(
        payment_provider="paypal",
        provider_event_id="WH-PP8-PENDING",
    )

    with Session(engine) as session:
        payment_event = session.scalar(
            select(InvoicePaymentEvent).where(
                InvoicePaymentEvent.payment_provider == "paypal",
                InvoicePaymentEvent.provider_event_id == "WH-PP8-PENDING",
            )
        )

    assert result.outcome == "unmatched"
    assert reconciliation.outcome == "pending"
    assert reconciliation.reason == RECONCILE_REASON_MISSING_PROVIDER_ACCOUNT_ID
    assert payment_event is not None
    assert payment_event.status == "unmatched"
    assert payment_event.provider_account_id is None
    assert payment_event.invoice_id is None


def test_summarize_paid_revenue_groups_by_tid_and_unattributed_reasons():
    engine = _engine()
    service = InvoicePaymentEventService(session_factory=lambda: Session(engine))

    with Session(engine) as session:
        creator = _create_creator(
            session,
            name="Story 49 Summary Creator",
            stripe_account_id="acct_story49_summary",
        )
        content_a, booking_a = _create_booking_chain(session, creator=creator, suffix="summary_a")
        invoice_a = _create_invoice(
            session,
            creator=creator,
            booking=booking_a,
            tid=content_a.tid,
            stripe_invoice_id="in_story49_summary_a",
            amount_cents=10000,
            status="paid",
        )
        invoice_a.paid_at = datetime(2026, 3, 8, 23, 10, tzinfo=timezone.utc)
        session.add(
            InvoicePaymentEvent(
                stripe_event_id="evt_story49_summary_a",
                stripe_event_type="invoice.paid",
                stripe_account_id=creator.stripe_account_id,
                stripe_invoice_id=invoice_a.stripe_invoice_id,
                invoice_id=invoice_a.id,
                creator_id=creator.id,
                booking_id=booking_a.id,
                tid=content_a.tid,
                status="applied",
                paid_at=invoice_a.paid_at,
                received_at=invoice_a.paid_at,
                processed_at=invoice_a.paid_at,
            )
        )

        content_b, booking_b = _create_booking_chain(session, creator=creator, suffix="summary_b")
        invoice_b = _create_invoice(
            session,
            creator=creator,
            booking=booking_b,
            tid=content_b.tid,
            stripe_invoice_id="in_story49_summary_b",
            amount_cents=25000,
            status="paid",
        )
        invoice_b.paid_at = datetime(2026, 3, 8, 23, 20, tzinfo=timezone.utc)
        session.add(
            InvoicePaymentEvent(
                stripe_event_id="evt_story49_summary_b",
                stripe_event_type="invoice.paid",
                stripe_account_id=creator.stripe_account_id,
                stripe_invoice_id=invoice_b.stripe_invoice_id,
                invoice_id=invoice_b.id,
                creator_id=creator.id,
                booking_id=booking_b.id,
                tid=content_b.tid,
                status="reconciled",
                paid_at=invoice_b.paid_at,
                received_at=invoice_b.paid_at,
                processed_at=invoice_b.paid_at,
            )
        )

        content_c, booking_c = _create_booking_chain(session, creator=creator, suffix="summary_c")
        invoice_c = _create_invoice(
            session,
            creator=creator,
            booking=booking_c,
            tid=content_c.tid,
            stripe_invoice_id="in_story49_summary_c",
            amount_cents=7000,
            status="open",
        )
        session.add(
            InvoicePaymentEvent(
                stripe_event_id="evt_story49_summary_c",
                stripe_event_type="invoice.paid",
                stripe_account_id=creator.stripe_account_id,
                stripe_invoice_id=invoice_c.stripe_invoice_id,
                invoice_id=invoice_c.id,
                creator_id=creator.id,
                booking_id=None,
                tid=None,
                status="unmatched",
                unattributed_reason=UNATTRIBUTED_REASON_MISSING_TID,
                paid_at=datetime(2026, 3, 8, 23, 25, tzinfo=timezone.utc),
                received_at=datetime(2026, 3, 8, 23, 25, tzinfo=timezone.utc),
                processed_at=None,
            )
        )
        session.add(
            InvoicePaymentEvent(
                stripe_event_id="evt_story49_summary_d",
                stripe_event_type="invoice.paid",
                stripe_account_id=creator.stripe_account_id,
                stripe_invoice_id="in_story49_summary_d",
                invoice_id=None,
                creator_id=creator.id,
                booking_id=None,
                tid=None,
                status="unmatched",
                unattributed_reason=UNATTRIBUTED_REASON_UNKNOWN_STRIPE_INVOICE_ID,
                paid_at=datetime(2026, 3, 8, 23, 26, tzinfo=timezone.utc),
                received_at=datetime(2026, 3, 8, 23, 26, tzinfo=timezone.utc),
                processed_at=None,
            )
        )

        other_creator = _create_creator(
            session,
            name="Story 49 Summary Noise Creator",
            stripe_account_id="acct_story49_summary_other",
        )
        other_content, other_booking = _create_booking_chain(
            session,
            creator=other_creator,
            suffix="summary_other",
        )
        other_invoice = _create_invoice(
            session,
            creator=other_creator,
            booking=other_booking,
            tid=other_content.tid,
            stripe_invoice_id="in_story49_summary_other",
            amount_cents=99999,
            status="paid",
        )
        other_invoice.paid_at = datetime(2026, 3, 8, 23, 27, tzinfo=timezone.utc)
        session.add(
            InvoicePaymentEvent(
                stripe_event_id="evt_story49_summary_other",
                stripe_event_type="invoice.paid",
                stripe_account_id=other_creator.stripe_account_id,
                stripe_invoice_id=other_invoice.stripe_invoice_id,
                invoice_id=other_invoice.id,
                creator_id=other_creator.id,
                booking_id=other_booking.id,
                tid=other_content.tid,
                status="applied",
                paid_at=other_invoice.paid_at,
                received_at=other_invoice.paid_at,
                processed_at=other_invoice.paid_at,
            )
        )

        creator_id = creator.id
        session.commit()

    summary = service.summarize_paid_revenue(creator_id=creator_id)

    assert summary.creator_id == creator_id
    assert [(item.tid, item.amount_cents) for item in summary.attributed_revenue_by_tid] == [
        ("story49_tid_summary_a", 10000),
        ("story49_tid_summary_b", 25000),
    ]
    assert summary.attributed_total_cents == 35000
    assert [
        (item.reason, item.amount_cents, item.event_count)
        for item in summary.unattributed_revenue_by_reason
    ] == [
        (UNATTRIBUTED_REASON_MISSING_TID, 7000, 1),
        (UNATTRIBUTED_REASON_UNKNOWN_STRIPE_INVOICE_ID, 0, 1),
    ]
    assert summary.unattributed_total_cents == 7000
    assert summary.unattributed_event_count == 2
