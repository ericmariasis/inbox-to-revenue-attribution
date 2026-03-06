import logging
from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_auth_user
from app.db.session import get_db
from app.models.auth_user import AuthUser
from app.models.booking_link import BookingLink
from app.schemas.booking_link import BookingLinkCreateRequest, BookingLinkResponse

router = APIRouter(prefix="/booking-links", tags=["booking-links"])
logger = logging.getLogger(__name__)


def _creator_scoped_booking_links_query(*, creator_id: UUID) -> Select[tuple[BookingLink]]:
    return (
        select(BookingLink)
        .where(BookingLink.creator_id == creator_id)
        .order_by(BookingLink.name.asc(), BookingLink.id.asc())
    )


def _build_booking_link_response(booking_link: BookingLink) -> BookingLinkResponse:
    return BookingLinkResponse(
        id=str(booking_link.id),
        name=booking_link.name,
        calendly_url=booking_link.calendly_url,
    )


@router.get("", response_model=list[BookingLinkResponse])
def list_booking_links(
    current_user: AuthUser = Depends(get_current_auth_user),
    db: Session = Depends(get_db),
) -> list[BookingLinkResponse]:
    booking_links = db.execute(
        _creator_scoped_booking_links_query(creator_id=current_user.creator_id)
    ).scalars()

    logger.info("booking_links_listed")

    return [_build_booking_link_response(booking_link) for booking_link in booking_links]


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

    return _build_booking_link_response(booking_link)
