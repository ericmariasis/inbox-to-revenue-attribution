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


def test_invoice_row_can_persist_against_canonical_booking():
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    issued_at = datetime(2026, 3, 8, 15, 0, tzinfo=timezone.utc)

    with Session(engine) as session:
        creator, _, content, booking = _create_creator_booking_link_content_and_booking(session)
        session.add(
            Invoice(
                creator_id=creator.id,
                booking_id=booking.id,
                tid=content.tid,
                stripe_account_id="acct_story43_primary",
                stripe_invoice_id="in_story43_primary",
                amount_cents=15000,
                currency="USD",
                status="open",
                issued_at=issued_at,
            )
        )
        session.commit()

        fetched = session.scalar(select(Invoice).where(Invoice.stripe_invoice_id == "in_story43_primary"))

        assert fetched is not None
        assert fetched.creator_id == creator.id
        assert fetched.booking_id == booking.id
        assert fetched.tid == content.tid
        assert fetched.stripe_account_id == "acct_story43_primary"
        assert fetched.stripe_invoice_id == "in_story43_primary"
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
            Invoice(
                creator_id=creator.id,
                booking_id=booking.id,
                tid=content.tid,
                stripe_account_id="acct_story43_duplicate",
                stripe_invoice_id="in_story43_duplicate",
                amount_cents=15000,
                currency="USD",
                status="open",
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
            Invoice(
                creator_id=creator.id,
                booking_id=second_booking.id,
                tid=second_content.tid,
                stripe_account_id="acct_story43_duplicate",
                stripe_invoice_id="in_story43_duplicate",
                amount_cents=17500,
                currency="USD",
                status="open",
                issued_at=datetime(2026, 3, 8, 16, 35, tzinfo=timezone.utc),
            )
        )

        with pytest.raises(IntegrityError):
            session.commit()

        session.rollback()

        rows = session.scalars(select(Invoice).where(Invoice.stripe_invoice_id == "in_story43_duplicate")).all()
        assert len(rows) == 1


def test_duplicate_invoice_for_same_booking_is_blocked_by_db_constraint():
    engine = create_engine(os.environ["TEST_DATABASE_URL"])

    with Session(engine) as session:
        creator, _, content, booking = _create_creator_booking_link_content_and_booking(
            session,
            booking_uuid="cal_booking_story43_duplicate_booking",
        )
        session.add(
            Invoice(
                creator_id=creator.id,
                booking_id=booking.id,
                tid=content.tid,
                stripe_account_id="acct_story43_same_booking",
                stripe_invoice_id="in_story43_same_booking_primary",
                amount_cents=15000,
                currency="USD",
                status="open",
                issued_at=datetime(2026, 3, 8, 17, 0, tzinfo=timezone.utc),
            )
        )
        session.commit()

        session.add(
            Invoice(
                creator_id=creator.id,
                booking_id=booking.id,
                tid=content.tid,
                stripe_account_id="acct_story43_same_booking",
                stripe_invoice_id="in_story43_same_booking_duplicate",
                amount_cents=15000,
                currency="USD",
                status="open",
                issued_at=datetime(2026, 3, 8, 17, 5, tzinfo=timezone.utc),
            )
        )

        with pytest.raises(IntegrityError):
            session.commit()

        session.rollback()

        rows = session.scalars(select(Invoice).where(Invoice.booking_id == booking.id)).all()
        assert len(rows) == 1
