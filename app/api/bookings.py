import logging
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy.orm import Session

from app.services.booking_attribution import list_creator_booking_attribution_rows

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BookingActivityResponse:
    id: str
    booking_link_id: str
    booking_link_name: str
    source_url: str | None
    tid: str | None
    status: str
    attribution_status: str
    attribution_reason: str | None
    booked_at: datetime
    canceled_at: datetime | None


def list_booking_activity_responses_for_creator(
    *,
    creator_id: UUID,
    db: Session,
) -> list[BookingActivityResponse]:
    rows = list_creator_booking_attribution_rows(
        creator_id=creator_id,
        db=db,
    )

    logger.info("booking_activity_listed")

    return [
        BookingActivityResponse(
            id=str(row.booking_id),
            booking_link_id=str(row.booking_link_id),
            booking_link_name=row.booking_link_name,
            source_url=row.source_url,
            tid=row.attribution.tid,
            status=row.booking_status,
            attribution_status=row.attribution.status,
            attribution_reason=row.attribution.unattributed_reason,
            booked_at=row.booked_at,
            canceled_at=row.canceled_at,
        )
        for row in rows
    ]
