import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import Select, and_, select
from sqlalchemy.orm import Session

from app.models.booking import Booking
from app.models.booking_link import BookingLink
from app.models.content import Content


BOOKING_ATTRIBUTION_STATUS_ATTRIBUTED = "attributed"
BOOKING_ATTRIBUTION_STATUS_UNATTRIBUTED = "unattributed"

BOOKING_UNATTRIBUTED_REASON_MISSING_TID = "MISSING_TID"
BOOKING_UNATTRIBUTED_REASON_UNKNOWN_TID = "UNKNOWN_TID"

BookingAttributionStatus = Literal["attributed", "unattributed"]
BookingUnattributedReason = Literal["MISSING_TID", "UNKNOWN_TID"]


@dataclass(frozen=True)
class BookingAttributionCurrentState:
    status: BookingAttributionStatus
    unattributed_reason: BookingUnattributedReason | None
    tid: str | None


@dataclass(frozen=True)
class CreatorBookingAttributionRow:
    booking_id: uuid.UUID
    booking_link_id: uuid.UUID
    booking_link_name: str
    source_url: str | None
    booking_status: str
    booked_at: datetime
    canceled_at: datetime | None
    attribution: BookingAttributionCurrentState


def get_booking_attribution_current_state(*, booking: Booking) -> BookingAttributionCurrentState:
    if booking.attribution_status == BOOKING_ATTRIBUTION_STATUS_UNATTRIBUTED:
        return BookingAttributionCurrentState(
            status=BOOKING_ATTRIBUTION_STATUS_UNATTRIBUTED,
            unattributed_reason=booking.unattributed_reason,
            tid=None,
        )

    return BookingAttributionCurrentState(
        status=BOOKING_ATTRIBUTION_STATUS_ATTRIBUTED,
        unattributed_reason=None,
        tid=booking.tid,
    )


def list_creator_booking_attribution_rows(
    *,
    creator_id: uuid.UUID,
    db: Session,
) -> list[CreatorBookingAttributionRow]:
    rows = db.execute(
        _creator_scoped_booking_attribution_query(creator_id=creator_id)
    ).all()

    return [
        CreatorBookingAttributionRow(
            booking_id=booking.id,
            booking_link_id=booking.booking_link_id,
            booking_link_name=booking_link_name,
            source_url=source_url,
            booking_status=booking.status,
            booked_at=booking.booked_at,
            canceled_at=booking.canceled_at,
            attribution=get_booking_attribution_current_state(booking=booking),
        )
        for booking, source_url, booking_link_name in rows
    ]


def _creator_scoped_booking_attribution_query(
    *,
    creator_id: uuid.UUID,
) -> Select[tuple[Booking, str | None, str]]:
    return (
        select(Booking, Content.source_url, BookingLink.name)
        .select_from(Booking)
        .outerjoin(
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
