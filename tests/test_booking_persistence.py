import os
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.booking import Booking
from app.models.booking_link import BookingLink
from app.models.booking_provider import BOOKING_PROVIDER_FULLSCOPE
from app.models.content import Content
from app.models.creator import Creator
from app.services.booking_attribution import (
    BOOKING_ATTRIBUTION_STATUS_UNATTRIBUTED,
    BOOKING_UNATTRIBUTED_REASON_MISSING_TID,
)


def _create_creator_booking_link_and_content(session: Session) -> tuple[Creator, BookingLink, Content]:
    creator = Creator(name="Booking Story 31 Creator")
    session.add(creator)
    session.flush()

    booking_link = BookingLink(
        creator_id=creator.id,
        name="Booking Story 31 Call",
        calendly_url="https://calendly.com/example/story31-call",
    )
    session.add(booking_link)
    session.flush()

    content = Content(
        creator_id=creator.id,
        booking_link_id=booking_link.id,
        source_url="https://example.com/posts/story-31-booking",
        tid="story31_tid",
    )
    session.add(content)
    session.flush()

    return creator, booking_link, content


def test_booking_row_can_persist_against_creator_owned_content():
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    booked_at = datetime(2026, 3, 7, 14, 30, tzinfo=timezone.utc)

    with Session(engine) as session:
        creator, booking_link, content = _create_creator_booking_link_and_content(session)
        session.add(
            Booking(
                creator_id=creator.id,
                tid=content.tid,
                booking_link_id=booking_link.id,
                calendly_booking_uuid="cal_booking_story31_primary",
                email="booked@example.com",
                status="created",
                booked_at=booked_at,
            )
        )
        session.commit()

        fetched = session.scalar(
            select(Booking).where(Booking.calendly_booking_uuid == "cal_booking_story31_primary")
        )

        assert fetched is not None
        assert fetched.creator_id == creator.id
        assert fetched.booking_link_id == booking_link.id
        assert fetched.tid == content.tid
        assert fetched.email == "booked@example.com"
        assert fetched.provider == "calendly"
        assert fetched.provider_booking_id == "cal_booking_story31_primary"
        assert fetched.calendly_booking_uuid == "cal_booking_story31_primary"
        assert fetched.status == "created"
        assert fetched.attribution_status == "attributed"
        assert fetched.unattributed_reason is None
        assert fetched.frozen_billing_amount_cents is None
        assert fetched.frozen_billing_currency is None
        assert fetched.booked_at == booked_at
        assert fetched.canceled_at is None
        assert fetched.creator is not None
        assert fetched.booking_link is not None
        assert fetched.content is not None
        assert fetched.content.tid == content.tid


def test_duplicate_calendly_booking_uuid_is_blocked_by_db_constraint():
    engine = create_engine(os.environ["TEST_DATABASE_URL"])

    with Session(engine) as session:
        creator, booking_link, content = _create_creator_booking_link_and_content(session)
        session.add(
            Booking(
                creator_id=creator.id,
                tid=content.tid,
                booking_link_id=booking_link.id,
                calendly_booking_uuid="cal_booking_story31_duplicate",
                email="first@example.com",
                status="created",
                booked_at=datetime(2026, 3, 7, 16, 0, tzinfo=timezone.utc),
            )
        )
        session.commit()

        session.add(
            Booking(
                creator_id=creator.id,
                tid=content.tid,
                booking_link_id=booking_link.id,
                calendly_booking_uuid="cal_booking_story31_duplicate",
                email="second@example.com",
                status="created",
                booked_at=datetime(2026, 3, 7, 17, 0, tzinfo=timezone.utc),
            )
        )

        with pytest.raises(IntegrityError):
            session.commit()

        session.rollback()

        rows = session.scalars(
            select(Booking).where(Booking.calendly_booking_uuid == "cal_booking_story31_duplicate")
        ).all()
        assert len(rows) == 1


def test_unattributed_booking_row_can_persist_with_explicit_reason():
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    booked_at = datetime(2026, 3, 7, 19, 30, tzinfo=timezone.utc)

    with Session(engine) as session:
        creator, booking_link, _ = _create_creator_booking_link_and_content(session)
        session.add(
            Booking(
                creator_id=creator.id,
                booking_link_id=booking_link.id,
                tid=None,
                calendly_booking_uuid="cal_booking_story78_unattributed",
                email="unattributed@example.com",
                status="created",
                attribution_status=BOOKING_ATTRIBUTION_STATUS_UNATTRIBUTED,
                unattributed_reason=BOOKING_UNATTRIBUTED_REASON_MISSING_TID,
                booked_at=booked_at,
            )
        )
        session.commit()

        fetched = session.scalar(
            select(Booking).where(
                Booking.calendly_booking_uuid == "cal_booking_story78_unattributed"
            )
        )

        assert fetched is not None
        assert fetched.creator_id == creator.id
        assert fetched.booking_link_id == booking_link.id
        assert fetched.tid is None
        assert fetched.attribution_status == BOOKING_ATTRIBUTION_STATUS_UNATTRIBUTED
        assert fetched.unattributed_reason == BOOKING_UNATTRIBUTED_REASON_MISSING_TID
        assert fetched.booked_at == booked_at
        assert fetched.content is None
        assert fetched.provider == "calendly"
        assert fetched.provider_booking_id == "cal_booking_story78_unattributed"


def test_same_provider_booking_identifier_can_coexist_across_different_providers():
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    shared_identifier = "shared_booking_identity_story_fs2"

    with Session(engine) as session:
        creator, booking_link, content = _create_creator_booking_link_and_content(session)
        fullscope_booking_link = BookingLink(
            creator_id=creator.id,
            name="FullScope Service Calendar",
            provider=BOOKING_PROVIDER_FULLSCOPE,
            destination_url="https://links.fullscope.tools/widget/bookings/fs2-direct-service",
            calendly_url=None,
        )
        session.add(fullscope_booking_link)
        session.flush()

        fullscope_content = Content(
            creator_id=creator.id,
            booking_link_id=fullscope_booking_link.id,
            source_url="https://example.com/posts/story-31-fullscope-booking",
            tid="story31_fullscope_tid",
        )
        session.add(fullscope_content)
        session.flush()

        session.add_all(
            [
                Booking(
                    creator_id=creator.id,
                    tid=content.tid,
                    booking_link_id=booking_link.id,
                    calendly_booking_uuid=shared_identifier,
                    email="calendly@example.com",
                    status="created",
                    booked_at=datetime(2026, 3, 7, 18, 0, tzinfo=timezone.utc),
                ),
                Booking(
                    creator_id=creator.id,
                    tid=fullscope_content.tid,
                    booking_link_id=fullscope_booking_link.id,
                    provider=BOOKING_PROVIDER_FULLSCOPE,
                    provider_booking_id=shared_identifier,
                    calendly_booking_uuid=None,
                    email="fullscope@example.com",
                    status="created",
                    booked_at=datetime(2026, 3, 7, 18, 30, tzinfo=timezone.utc),
                ),
            ]
        )
        session.commit()

        rows = session.scalars(
            select(Booking)
            .where(Booking.provider_booking_id == shared_identifier)
            .order_by(Booking.provider.asc())
        ).all()

    assert [(row.provider, row.provider_booking_id) for row in rows] == [
        ("calendly", shared_identifier),
        (BOOKING_PROVIDER_FULLSCOPE, shared_identifier),
    ]
