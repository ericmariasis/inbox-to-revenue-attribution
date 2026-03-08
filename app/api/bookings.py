import logging
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import Select, and_, select
from sqlalchemy.orm import Session

from app.models.booking import Booking
from app.models.booking_link import BookingLink
from app.models.content import Content

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BookingActivityResponse:
    id: str
    booking_link_id: str
    booking_link_name: str
    source_url: str
    tid: str
    status: str
    booked_at: datetime
    canceled_at: datetime | None


def _creator_scoped_booking_activity_query(
    *,
    creator_id: UUID,
) -> Select[tuple[Booking, str, str]]:
    return (
        select(Booking, Content.source_url, BookingLink.name)
        .join(
            Content,
            and_(
                Content.tid == Booking.tid,
                Content.creator_id == creator_id,
            ),
        )
        .join(
            BookingLink,
            and_(
                BookingLink.id == Booking.booking_link_id,
                BookingLink.creator_id == creator_id,
            ),
        )
        .where(Booking.creator_id == creator_id)
        .order_by(Booking.booked_at.desc(), Booking.id.desc())
    )


def list_booking_activity_responses_for_creator(
    *,
    creator_id: UUID,
    db: Session,
) -> list[BookingActivityResponse]:
    rows = db.execute(
        _creator_scoped_booking_activity_query(creator_id=creator_id)
    ).all()

    logger.info("booking_activity_listed")

    return [
        BookingActivityResponse(
            id=str(booking.id),
            booking_link_id=str(booking.booking_link_id),
            booking_link_name=booking_link_name,
            source_url=source_url,
            tid=booking.tid,
            status=booking.status,
            booked_at=booking.booked_at,
            canceled_at=booking.canceled_at,
        )
        for booking, source_url, booking_link_name in rows
    ]
