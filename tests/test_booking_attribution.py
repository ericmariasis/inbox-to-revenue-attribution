import os
from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models.booking import Booking
from app.models.booking_link import BookingLink
from app.models.content import Content
from app.models.creator import Creator
from app.services.booking_attribution import (
    BOOKING_ATTRIBUTION_STATUS_UNATTRIBUTED,
    BOOKING_UNATTRIBUTED_REASON_MISSING_TID,
    get_booking_attribution_current_state,
    list_creator_booking_attribution_rows,
)


def _engine():
    return create_engine(os.environ["TEST_DATABASE_URL"])


def test_get_booking_attribution_current_state_distinguishes_attributed_and_unattributed_rows():
    engine = _engine()

    with Session(engine) as session:
        creator = Creator(name="Booking Attribution Creator")
        session.add(creator)
        session.flush()

        booking_link = BookingLink(
            creator_id=creator.id,
            name="Booking Attribution Link",
            calendly_url="https://calendly.com/example/booking-attribution-link",
        )
        session.add(booking_link)
        session.flush()

        content = Content(
            creator_id=creator.id,
            booking_link_id=booking_link.id,
            source_url="https://example.com/posts/booking-attribution",
            tid="booking_attribution_tid",
        )
        session.add(content)
        session.flush()

        attributed_booking = Booking(
            creator_id=creator.id,
            booking_link_id=booking_link.id,
            tid=content.tid,
            calendly_booking_uuid="BOOK_ATTRIBUTION_ATTRIBUTED",
            email="attributed@example.com",
            status="created",
            booked_at=datetime(2026, 3, 12, 14, 0, tzinfo=timezone.utc),
        )
        unattributed_booking = Booking(
            creator_id=creator.id,
            booking_link_id=booking_link.id,
            tid=None,
            calendly_booking_uuid="BOOK_ATTRIBUTION_UNATTRIBUTED",
            email="unattributed@example.com",
            status="created",
            attribution_status=BOOKING_ATTRIBUTION_STATUS_UNATTRIBUTED,
            unattributed_reason=BOOKING_UNATTRIBUTED_REASON_MISSING_TID,
            booked_at=datetime(2026, 3, 12, 15, 0, tzinfo=timezone.utc),
        )
        session.add_all([attributed_booking, unattributed_booking])
        session.commit()
        attributed_booking_id = attributed_booking.id
        unattributed_booking_id = unattributed_booking.id

    with Session(engine) as session:
        attributed = session.get(Booking, attributed_booking_id)
        unattributed = session.get(Booking, unattributed_booking_id)
        assert attributed is not None
        assert unattributed is not None

        attributed_state = get_booking_attribution_current_state(booking=attributed)
        unattributed_state = get_booking_attribution_current_state(booking=unattributed)

    assert attributed_state.status == "attributed"
    assert attributed_state.tid == "booking_attribution_tid"
    assert attributed_state.unattributed_reason is None
    assert unattributed_state.status == "unattributed"
    assert unattributed_state.tid is None
    assert unattributed_state.unattributed_reason == BOOKING_UNATTRIBUTED_REASON_MISSING_TID


def test_list_creator_booking_attribution_rows_keeps_unattributed_booking_visible():
    engine = _engine()

    with Session(engine) as session:
        creator = Creator(name="Creator Booking Activity")
        other_creator = Creator(name="Other Creator Booking Activity")
        session.add_all([creator, other_creator])
        session.flush()

        booking_link = BookingLink(
            creator_id=creator.id,
            name="Creator Booking Activity Link",
            calendly_url="https://calendly.com/example/creator-booking-activity",
        )
        other_booking_link = BookingLink(
            creator_id=other_creator.id,
            name="Other Creator Booking Activity Link",
            calendly_url="https://calendly.com/example/other-booking-activity",
        )
        session.add_all([booking_link, other_booking_link])
        session.flush()

        content = Content(
            creator_id=creator.id,
            booking_link_id=booking_link.id,
            source_url="https://example.com/posts/creator-booking-activity",
            tid="creator_booking_activity_tid",
        )
        other_content = Content(
            creator_id=other_creator.id,
            booking_link_id=other_booking_link.id,
            source_url="https://example.com/posts/other-booking-activity",
            tid="other_booking_activity_tid",
        )
        session.add_all([content, other_content])
        session.flush()

        session.add_all(
            [
                Booking(
                    creator_id=creator.id,
                    booking_link_id=booking_link.id,
                    tid=content.tid,
                    calendly_booking_uuid="BOOK_ACTIVITY_ATTRIBUTED",
                    email="activity-attributed@example.com",
                    status="created",
                    booked_at=datetime(2026, 3, 12, 14, 0, tzinfo=timezone.utc),
                ),
                Booking(
                    creator_id=creator.id,
                    booking_link_id=booking_link.id,
                    tid=None,
                    calendly_booking_uuid="BOOK_ACTIVITY_UNATTRIBUTED",
                    email="activity-unattributed@example.com",
                    status="created",
                    attribution_status=BOOKING_ATTRIBUTION_STATUS_UNATTRIBUTED,
                    unattributed_reason=BOOKING_UNATTRIBUTED_REASON_MISSING_TID,
                    booked_at=datetime(2026, 3, 12, 15, 0, tzinfo=timezone.utc),
                ),
                Booking(
                    creator_id=other_creator.id,
                    booking_link_id=other_booking_link.id,
                    tid=other_content.tid,
                    calendly_booking_uuid="BOOK_ACTIVITY_HIDDEN",
                    email="activity-hidden@example.com",
                    status="created",
                    booked_at=datetime(2026, 3, 12, 16, 0, tzinfo=timezone.utc),
                ),
            ]
        )
        session.commit()

        creator_id = creator.id

    with Session(engine) as session:
        rows = list_creator_booking_attribution_rows(
            creator_id=creator_id,
            db=session,
        )

    assert [row.attribution.status for row in rows] == ["unattributed", "attributed"]
    assert rows[0].booking_link_name == "Creator Booking Activity Link"
    assert rows[0].source_url is None
    assert rows[0].attribution.unattributed_reason == BOOKING_UNATTRIBUTED_REASON_MISSING_TID
    assert rows[1].source_url == "https://example.com/posts/creator-booking-activity"
    assert rows[1].attribution.tid == "creator_booking_activity_tid"
