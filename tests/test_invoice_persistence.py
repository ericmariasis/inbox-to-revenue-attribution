import os
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.booking import Booking
from app.models.booking_link import BookingLink
from app.models.content import Content
from app.models.creator import Creator
from app.models.invoice import Invoice
from app.models.invoice_payment_event import InvoicePaymentEvent


def _create_creator_booking_link_content_and_booking(
    session: Session,
    *,
    booking_uuid: str = "cal_booking_story43_primary",
    tid: str = "story43_tid",
    booked_at: datetime | None = None,
) -> tuple[Creator, BookingLink, Content, Booking]:
    creator = Creator(
        name="Invoice Story 43 Creator",
        stripe_connect_status="connected",
        stripe_account_id="acct_story43_primary",
    )
    session.add(creator)
    session.flush()

    booking_link = BookingLink(
        creator_id=creator.id,
        name="Invoice Story 43 Call",
        calendly_url="https://calendly.com/example/story43-call",
    )
    session.add(booking_link)
    session.flush()

    content = Content(
        creator_id=creator.id,
        booking_link_id=booking_link.id,
        source_url="https://example.com/posts/story-43-invoice",
        tid=tid,
    )
    session.add(content)
    session.flush()

    booking = Booking(
        creator_id=creator.id,
        tid=content.tid,
        booking_link_id=booking_link.id,
        calendly_booking_uuid=booking_uuid,
        email="booked@example.com",
        status="created",
        booked_at=booked_at or datetime(2026, 3, 8, 14, 30, tzinfo=timezone.utc),
    )
    session.add(booking)
    session.flush()

    return creator, booking_link, content, booking


def _create_invoice(
    *,
    creator: Creator,
    content: Content,
    booking: Booking,
    stripe_account_id: str = "acct_story43_primary",
    stripe_invoice_id: str = "in_story43_primary",
    amount_cents: int = 15000,
    currency: str = "USD",
    status: str = "open",
    issued_at: datetime | None = None,
) -> Invoice:
    return Invoice(
        creator_id=creator.id,
        booking_id=booking.id,
        tid=content.tid,
        stripe_account_id=stripe_account_id,
        stripe_invoice_id=stripe_invoice_id,
        amount_cents=amount_cents,
        currency=currency,
        status=status,
        issued_at=issued_at or datetime(2026, 3, 8, 15, 0, tzinfo=timezone.utc),
    )


def test_invoice_row_can_persist_against_canonical_booking():
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    issued_at = datetime(2026, 3, 8, 15, 0, tzinfo=timezone.utc)

    with Session(engine) as session:
        creator, _, content, booking = _create_creator_booking_link_content_and_booking(session)
        session.add(
            _create_invoice(
                creator=creator,
                content=content,
                booking=booking,
                issued_at=issued_at,
            )
        )
        session.commit()

        fetched = session.scalar(select(Invoice).where(Invoice.stripe_invoice_id == "in_story43_primary"))

        assert fetched is not None
        assert fetched.creator_id == creator.id
        assert fetched.booking_id == booking.id
        assert fetched.tid == content.tid
        assert fetched.payment_provider == "stripe"
        assert fetched.provider_account_id == "acct_story43_primary"
        assert fetched.provider_invoice_id == "in_story43_primary"
        assert fetched.stripe_account_id == "acct_story43_primary"
        assert fetched.stripe_invoice_id == "in_story43_primary"
        assert fetched.resolved_payment_provider == "stripe"
        assert fetched.resolved_provider_account_id == "acct_story43_primary"
        assert fetched.resolved_provider_invoice_id == "in_story43_primary"
        assert fetched.amount_cents == 15000
        assert fetched.currency == "USD"
        assert fetched.status == "open"
        assert fetched.issued_at == issued_at
        assert fetched.paid_at is None
        assert fetched.voided_at is None
        assert fetched.creator is not None
        assert fetched.booking is not None
        assert fetched.content is not None
        assert fetched.booking.invoice is not None
        assert fetched.booking.invoice.id == fetched.id


def test_duplicate_stripe_invoice_id_is_blocked_by_db_constraint():
    engine = create_engine(os.environ["TEST_DATABASE_URL"])

    with Session(engine) as session:
        creator, _, content, booking = _create_creator_booking_link_content_and_booking(
            session,
            booking_uuid="cal_booking_story43_duplicate_invoice_a",
            tid="story43_tid_a",
            booked_at=datetime(2026, 3, 8, 16, 0, tzinfo=timezone.utc),
        )
        session.add(
            _create_invoice(
                creator=creator,
                content=content,
                booking=booking,
                stripe_account_id="acct_story43_duplicate",
                stripe_invoice_id="in_story43_duplicate",
                issued_at=datetime(2026, 3, 8, 16, 5, tzinfo=timezone.utc),
            )
        )
        session.commit()

        _, _, second_content, second_booking = _create_creator_booking_link_content_and_booking(
            session,
            booking_uuid="cal_booking_story43_duplicate_invoice_b",
            tid="story43_tid_b",
            booked_at=datetime(2026, 3, 8, 16, 30, tzinfo=timezone.utc),
        )
        session.add(
            _create_invoice(
                creator=creator,
                content=second_content,
                booking=second_booking,
                stripe_account_id="acct_story43_duplicate",
                stripe_invoice_id="in_story43_duplicate",
                amount_cents=17500,
                issued_at=datetime(2026, 3, 8, 16, 35, tzinfo=timezone.utc),
            )
        )

        with pytest.raises(IntegrityError):
            session.commit()

        session.rollback()

        rows = session.scalars(select(Invoice).where(Invoice.stripe_invoice_id == "in_story43_duplicate")).all()
        assert len(rows) == 1


def test_duplicate_provider_invoice_identity_is_blocked_by_db_constraint():
    engine = create_engine(os.environ["TEST_DATABASE_URL"])

    with Session(engine) as session:
        creator, _, content, booking = _create_creator_booking_link_content_and_booking(
            session,
            booking_uuid="pp2_provider_identity_a",
            tid="pp2_provider_identity_tid_a",
            booked_at=datetime(2026, 3, 8, 16, 40, tzinfo=timezone.utc),
        )
        session.add(
            Invoice(
                creator_id=creator.id,
                booking_id=booking.id,
                tid=content.tid,
                payment_provider="paypal",
                provider_account_id="merchant_pp2_duplicate",
                provider_invoice_id="INV-PP2-DUPLICATE",
                amount_cents=15000,
                currency="USD",
                status="open",
                issued_at=datetime(2026, 3, 8, 16, 45, tzinfo=timezone.utc),
            )
        )
        session.commit()

        second_creator, _, second_content, second_booking = _create_creator_booking_link_content_and_booking(
            session,
            booking_uuid="pp2_provider_identity_b",
            tid="pp2_provider_identity_tid_b",
            booked_at=datetime(2026, 3, 8, 16, 50, tzinfo=timezone.utc),
        )
        session.add(
            Invoice(
                creator_id=second_creator.id,
                booking_id=second_booking.id,
                tid=second_content.tid,
                payment_provider="paypal",
                provider_account_id="merchant_pp2_duplicate",
                provider_invoice_id="INV-PP2-DUPLICATE",
                amount_cents=17500,
                currency="USD",
                status="open",
                issued_at=datetime(2026, 3, 8, 16, 55, tzinfo=timezone.utc),
            )
        )

        with pytest.raises(IntegrityError):
            session.commit()

        session.rollback()

        rows = session.scalars(
            select(Invoice).where(
                Invoice.payment_provider == "paypal",
                Invoice.provider_account_id == "merchant_pp2_duplicate",
                Invoice.provider_invoice_id == "INV-PP2-DUPLICATE",
            )
        ).all()
        assert len(rows) == 1


def test_duplicate_invoice_for_same_booking_is_blocked_by_db_constraint():
    engine = create_engine(os.environ["TEST_DATABASE_URL"])

    with Session(engine) as session:
        creator, _, content, booking = _create_creator_booking_link_content_and_booking(
            session,
            booking_uuid="cal_booking_story43_duplicate_booking",
        )
        session.add(
            _create_invoice(
                creator=creator,
                content=content,
                booking=booking,
                stripe_account_id="acct_story43_same_booking",
                stripe_invoice_id="in_story43_same_booking_primary",
                issued_at=datetime(2026, 3, 8, 17, 0, tzinfo=timezone.utc),
            )
        )
        session.commit()

        session.add(
            _create_invoice(
                creator=creator,
                content=content,
                booking=booking,
                stripe_account_id="acct_story43_same_booking",
                stripe_invoice_id="in_story43_same_booking_duplicate",
                issued_at=datetime(2026, 3, 8, 17, 5, tzinfo=timezone.utc),
            )
        )

        with pytest.raises(IntegrityError):
            session.commit()

        session.rollback()

        rows = session.scalars(select(Invoice).where(Invoice.booking_id == booking.id)).all()
        assert len(rows) == 1


def test_invoice_payment_event_can_persist_against_canonical_invoice_chain():
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    paid_at = datetime(2026, 3, 8, 18, 0, tzinfo=timezone.utc)
    received_at = datetime(2026, 3, 8, 18, 1, tzinfo=timezone.utc)
    processed_at = datetime(2026, 3, 8, 18, 2, tzinfo=timezone.utc)

    with Session(engine) as session:
        creator, _, content, booking = _create_creator_booking_link_content_and_booking(
            session,
            booking_uuid="cal_booking_story47_primary",
            tid="story47_tid_primary",
        )
        invoice = _create_invoice(
            creator=creator,
            content=content,
            booking=booking,
            stripe_account_id="acct_story47_primary",
            stripe_invoice_id="in_story47_primary",
            issued_at=datetime(2026, 3, 8, 17, 30, tzinfo=timezone.utc),
        )
        session.add(invoice)
        session.flush()
        session.add(
            InvoicePaymentEvent(
                stripe_event_id="evt_story47_primary",
                stripe_event_type="invoice.paid",
                stripe_account_id="acct_story47_primary",
                stripe_invoice_id="in_story47_primary",
                invoice_id=invoice.id,
                creator_id=creator.id,
                booking_id=booking.id,
                tid=content.tid,
                status="applied",
                paid_at=paid_at,
                received_at=received_at,
                processed_at=processed_at,
            )
        )
        session.commit()

        fetched = session.scalar(
            select(InvoicePaymentEvent).where(
                InvoicePaymentEvent.stripe_event_id == "evt_story47_primary"
            )
        )

        assert fetched is not None
        assert fetched.payment_provider == "stripe"
        assert fetched.provider_event_id == "evt_story47_primary"
        assert fetched.provider_event_type == "invoice.paid"
        assert fetched.provider_account_id == "acct_story47_primary"
        assert fetched.provider_invoice_id == "in_story47_primary"
        assert fetched.stripe_event_id == "evt_story47_primary"
        assert fetched.stripe_event_type == "invoice.paid"
        assert fetched.stripe_account_id == "acct_story47_primary"
        assert fetched.stripe_invoice_id == "in_story47_primary"
        assert fetched.resolved_payment_provider == "stripe"
        assert fetched.resolved_provider_event_id == "evt_story47_primary"
        assert fetched.resolved_provider_event_type == "invoice.paid"
        assert fetched.resolved_provider_account_id == "acct_story47_primary"
        assert fetched.resolved_provider_invoice_id == "in_story47_primary"
        assert fetched.invoice_id == invoice.id
        assert fetched.creator_id == creator.id
        assert fetched.booking_id == booking.id
        assert fetched.tid == content.tid
        assert fetched.status == "applied"
        assert fetched.unattributed_reason is None
        assert fetched.paid_at == paid_at
        assert fetched.received_at == received_at
        assert fetched.processed_at == processed_at
        assert fetched.invoice is not None
        assert fetched.invoice.id == invoice.id
        assert fetched.invoice.payment_provider == "stripe"
        assert fetched.invoice.provider_account_id == "acct_story47_primary"
        assert fetched.invoice.provider_invoice_id == "in_story47_primary"
        assert fetched.creator is not None
        assert fetched.creator.id == creator.id
        assert fetched.booking is not None
        assert fetched.booking.id == booking.id
        assert fetched.content is not None
        assert fetched.content.tid == content.tid
        assert fetched.invoice.payment_events[0].id == fetched.id


def test_provider_neutral_payment_event_can_persist_without_legacy_stripe_identity():
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    paid_at = datetime(2026, 3, 8, 18, 10, tzinfo=timezone.utc)
    received_at = datetime(2026, 3, 8, 18, 11, tzinfo=timezone.utc)

    with Session(engine) as session:
        session.add(
            InvoicePaymentEvent(
                payment_provider="paypal",
                provider_event_id="WH-PP3-PERSIST-PRIMARY",
                provider_event_type="INVOICING.INVOICE.PAID",
                provider_account_id="merchant_pp3_persist",
                provider_invoice_id="INV-PP3-PERSIST-PRIMARY",
                invoice_id=None,
                creator_id=None,
                booking_id=None,
                tid=None,
                status="unmatched",
                unattributed_reason="UNKNOWN_STRIPE_INVOICE_ID",
                paid_at=paid_at,
                received_at=received_at,
                processed_at=None,
            )
        )
        session.commit()

        fetched = session.scalar(
            select(InvoicePaymentEvent).where(
                InvoicePaymentEvent.provider_event_id == "WH-PP3-PERSIST-PRIMARY"
            )
        )

        assert fetched is not None
        assert fetched.payment_provider == "paypal"
        assert fetched.provider_event_id == "WH-PP3-PERSIST-PRIMARY"
        assert fetched.provider_event_type == "INVOICING.INVOICE.PAID"
        assert fetched.provider_account_id == "merchant_pp3_persist"
        assert fetched.provider_invoice_id == "INV-PP3-PERSIST-PRIMARY"
        assert fetched.stripe_event_id is None
        assert fetched.stripe_event_type is None
        assert fetched.stripe_account_id is None
        assert fetched.stripe_invoice_id is None
        assert fetched.resolved_payment_provider == "paypal"
        assert fetched.resolved_provider_event_id == "WH-PP3-PERSIST-PRIMARY"
        assert fetched.resolved_provider_event_type == "INVOICING.INVOICE.PAID"
        assert fetched.resolved_provider_account_id == "merchant_pp3_persist"
        assert fetched.resolved_provider_invoice_id == "INV-PP3-PERSIST-PRIMARY"
        assert fetched.status == "unmatched"
        assert fetched.unattributed_reason == "UNKNOWN_STRIPE_INVOICE_ID"
        assert fetched.paid_at == paid_at
        assert fetched.received_at == received_at
        assert fetched.processed_at is None


def test_provider_neutral_invoice_can_persist_without_legacy_stripe_identity():
    engine = create_engine(os.environ["TEST_DATABASE_URL"])

    with Session(engine) as session:
        creator, _, content, booking = _create_creator_booking_link_content_and_booking(
            session,
            booking_uuid="pp2_provider_only_booking",
            tid="pp2_provider_only_tid",
            booked_at=datetime(2026, 3, 8, 18, 30, tzinfo=timezone.utc),
        )
        session.add(
            Invoice(
                creator_id=creator.id,
                booking_id=booking.id,
                tid=content.tid,
                payment_provider="paypal",
                provider_account_id="merchant_pp2_provider_only",
                provider_invoice_id="INV-PP2-PROVIDER-ONLY",
                amount_cents=21000,
                currency="USD",
                status="open",
                issued_at=datetime(2026, 3, 8, 18, 35, tzinfo=timezone.utc),
            )
        )
        session.commit()

        fetched = session.scalar(
            select(Invoice).where(Invoice.provider_invoice_id == "INV-PP2-PROVIDER-ONLY")
        )

        assert fetched is not None
        assert fetched.payment_provider == "paypal"
        assert fetched.provider_account_id == "merchant_pp2_provider_only"
        assert fetched.provider_invoice_id == "INV-PP2-PROVIDER-ONLY"
        assert fetched.stripe_account_id is None
        assert fetched.stripe_invoice_id is None
        assert fetched.resolved_payment_provider == "paypal"
        assert fetched.resolved_provider_account_id == "merchant_pp2_provider_only"
        assert fetched.resolved_provider_invoice_id == "INV-PP2-PROVIDER-ONLY"


def test_invoice_payment_event_can_persist_unmatched_state_with_null_local_linkage():
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    paid_at = datetime(2026, 3, 8, 19, 0, tzinfo=timezone.utc)
    received_at = datetime(2026, 3, 8, 19, 1, tzinfo=timezone.utc)

    with Session(engine) as session:
        session.add(
            InvoicePaymentEvent(
                stripe_event_id="evt_story47_unmatched",
                stripe_event_type="invoice.paid",
                stripe_account_id="acct_story47_unmatched",
                stripe_invoice_id="in_story47_unmatched",
                invoice_id=None,
                creator_id=None,
                booking_id=None,
                tid=None,
                status="unmatched",
                unattributed_reason="UNKNOWN_STRIPE_INVOICE_ID",
                paid_at=paid_at,
                received_at=received_at,
                processed_at=None,
            )
        )
        session.commit()

        fetched = session.scalar(
            select(InvoicePaymentEvent).where(
                InvoicePaymentEvent.stripe_event_id == "evt_story47_unmatched"
            )
        )

        assert fetched is not None
        assert fetched.payment_provider == "stripe"
        assert fetched.provider_event_id == "evt_story47_unmatched"
        assert fetched.provider_event_type == "invoice.paid"
        assert fetched.provider_account_id == "acct_story47_unmatched"
        assert fetched.provider_invoice_id == "in_story47_unmatched"
        assert fetched.invoice_id is None
        assert fetched.creator_id is None
        assert fetched.booking_id is None
        assert fetched.tid is None
        assert fetched.status == "unmatched"
        assert fetched.unattributed_reason == "UNKNOWN_STRIPE_INVOICE_ID"
        assert fetched.paid_at == paid_at
        assert fetched.received_at == received_at
        assert fetched.processed_at is None
        assert fetched.invoice is None
        assert fetched.creator is None
        assert fetched.booking is None
        assert fetched.content is None


def test_duplicate_invoice_payment_event_stripe_event_id_is_blocked_by_db_constraint():
    engine = create_engine(os.environ["TEST_DATABASE_URL"])

    with Session(engine) as session:
        session.add(
            InvoicePaymentEvent(
                stripe_event_id="evt_story47_duplicate",
                stripe_event_type="invoice.paid",
                stripe_account_id="acct_story47_duplicate_a",
                stripe_invoice_id="in_story47_duplicate_a",
                status="unmatched",
                unattributed_reason="UNKNOWN_STRIPE_INVOICE_ID",
                paid_at=datetime(2026, 3, 8, 20, 0, tzinfo=timezone.utc),
                received_at=datetime(2026, 3, 8, 20, 1, tzinfo=timezone.utc),
            )
        )
        session.commit()

        session.add(
            InvoicePaymentEvent(
                stripe_event_id="evt_story47_duplicate",
                stripe_event_type="invoice.paid",
                stripe_account_id="acct_story47_duplicate_b",
                stripe_invoice_id="in_story47_duplicate_b",
                status="unmatched",
                unattributed_reason="UNKNOWN_STRIPE_INVOICE_ID",
                paid_at=datetime(2026, 3, 8, 20, 2, tzinfo=timezone.utc),
                received_at=datetime(2026, 3, 8, 20, 3, tzinfo=timezone.utc),
            )
        )

        with pytest.raises(IntegrityError):
            session.commit()

        session.rollback()

        rows = session.scalars(
            select(InvoicePaymentEvent).where(
                InvoicePaymentEvent.stripe_event_id == "evt_story47_duplicate"
            )
        ).all()
        assert len(rows) == 1


def test_duplicate_provider_event_identity_is_blocked_by_db_constraint():
    engine = create_engine(os.environ["TEST_DATABASE_URL"])

    with Session(engine) as session:
        session.add(
            InvoicePaymentEvent(
                payment_provider="paypal",
                provider_event_id="WH-PP3-DUPLICATE",
                provider_event_type="INVOICING.INVOICE.PAID",
                provider_account_id="merchant_pp3_duplicate_a",
                provider_invoice_id="INV-PP3-DUPLICATE-A",
                status="unmatched",
                unattributed_reason="UNKNOWN_STRIPE_INVOICE_ID",
                paid_at=datetime(2026, 3, 8, 20, 4, tzinfo=timezone.utc),
                received_at=datetime(2026, 3, 8, 20, 5, tzinfo=timezone.utc),
            )
        )
        session.commit()

        session.add(
            InvoicePaymentEvent(
                payment_provider="paypal",
                provider_event_id="WH-PP3-DUPLICATE",
                provider_event_type="INVOICING.INVOICE.PAID",
                provider_account_id="merchant_pp3_duplicate_b",
                provider_invoice_id="INV-PP3-DUPLICATE-B",
                status="unmatched",
                unattributed_reason="UNKNOWN_STRIPE_INVOICE_ID",
                paid_at=datetime(2026, 3, 8, 20, 6, tzinfo=timezone.utc),
                received_at=datetime(2026, 3, 8, 20, 7, tzinfo=timezone.utc),
            )
        )

        with pytest.raises(IntegrityError):
            session.commit()

        session.rollback()

        rows = session.scalars(
            select(InvoicePaymentEvent).where(
                InvoicePaymentEvent.payment_provider == "paypal",
                InvoicePaymentEvent.provider_event_id == "WH-PP3-DUPLICATE",
            )
        ).all()
        assert len(rows) == 1
