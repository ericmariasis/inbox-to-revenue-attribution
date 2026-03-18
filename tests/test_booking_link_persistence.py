import os

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models.booking_link import BookingLink
from app.models.booking_provider import BOOKING_PROVIDER_FULLSCOPE
from app.models.creator import Creator


def test_creator_can_own_multiple_booking_links():
    engine = create_engine(os.environ["TEST_DATABASE_URL"])

    with Session(engine) as session:
        creator = Creator(name="Booking Link Creator")
        session.add(creator)
        session.flush()

        session.add_all(
            [
                BookingLink(
                    creator_id=creator.id,
                    name="Deep Dive Call",
                    calendly_url="https://calendly.com/example/deep-dive-call",
                    billing_amount_cents=15000,
                    billing_currency="USD",
                ),
                BookingLink(
                    creator_id=creator.id,
                    name="Free Consult",
                    calendly_url="https://calendly.com/example/free-consult",
                ),
            ]
        )
        session.commit()

        rows = session.scalars(
            select(BookingLink)
            .where(BookingLink.creator_id == creator.id)
            .order_by(BookingLink.name)
        ).all()

        assert [
            (
                row.name,
                row.provider,
                row.destination_url,
                row.calendly_url,
                row.billing_amount_cents,
                row.billing_currency,
            )
            for row in rows
        ] == [
            (
                "Deep Dive Call",
                "calendly",
                "https://calendly.com/example/deep-dive-call",
                "https://calendly.com/example/deep-dive-call",
                15000,
                "USD",
            ),
            (
                "Free Consult",
                "calendly",
                "https://calendly.com/example/free-consult",
                "https://calendly.com/example/free-consult",
                None,
                None,
            ),
        ]
        assert all(row.created_at is not None and row.updated_at is not None for row in rows)


def test_booking_link_can_persist_fullscope_destination_without_legacy_calendly_url():
    engine = create_engine(os.environ["TEST_DATABASE_URL"])

    with Session(engine) as session:
        creator = Creator(name="FullScope Booking Link Creator")
        session.add(creator)
        session.flush()

        booking_link = BookingLink(
            creator_id=creator.id,
            name="FullScope Personal Calendar",
            provider=BOOKING_PROVIDER_FULLSCOPE,
            destination_url="https://links.fullscope.tools/widget/bookings/fs1-personal-calendar",
            calendly_url=None,
        )
        session.add(booking_link)
        session.commit()

        fetched = session.scalar(select(BookingLink).where(BookingLink.id == booking_link.id))

    assert fetched is not None
    assert fetched.provider == BOOKING_PROVIDER_FULLSCOPE
    assert (
        fetched.destination_url
        == "https://links.fullscope.tools/widget/bookings/fs1-personal-calendar"
    )
    assert fetched.resolved_destination_url == fetched.destination_url
    assert fetched.calendly_url is None
