import logging

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_auth_user
from app.db.session import get_db
from app.models.auth_user import AuthUser
from app.models.booking_link import BookingLink
from app.schemas.booking_link import BookingLinkCreateRequest, BookingLinkResponse

router = APIRouter(prefix="/booking-links", tags=["booking-links"])
logger = logging.getLogger(__name__)


@router.post("", response_model=BookingLinkResponse, status_code=status.HTTP_201_CREATED)
def create_booking_link(
    payload: BookingLinkCreateRequest,
    current_user: AuthUser = Depends(get_current_auth_user),
    db: Session = Depends(get_db),
) -> BookingLinkResponse:
    booking_link = BookingLink(
        creator_id=current_user.creator_id,
        name=payload.name,
        calendly_url=payload.calendly_url,
    )
    db.add(booking_link)
    db.commit()
    db.refresh(booking_link)

    logger.info("booking_link_created")

    return BookingLinkResponse(
        id=str(booking_link.id),
        name=booking_link.name,
        calendly_url=booking_link.calendly_url,
    )
