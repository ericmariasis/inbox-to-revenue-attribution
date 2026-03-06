import logging
import uuid
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_auth_user
from app.core.config import get_settings
from app.db.session import get_db
from app.models.auth_user import AuthUser
from app.models.booking_link import BookingLink
from app.models.content import Content
from app.schemas.content import ContentCreateRequest, ContentResponse

router = APIRouter(prefix="/content", tags=["content"])
logger = logging.getLogger(__name__)


def _creator_owned_booking_link_query(
    *,
    booking_link_id: UUID,
    creator_id: UUID,
) -> Select[tuple[BookingLink]]:
    return select(BookingLink).where(
        BookingLink.id == booking_link_id,
        BookingLink.creator_id == creator_id,
    )


def _tracked_url_for_tid(tid: str) -> str:
    base_url = get_settings().tracked_link_base_url.rstrip("/")
    return f"{base_url}/r/{tid}"


def _build_content_response(content: Content) -> ContentResponse:
    return ContentResponse(
        id=str(content.id),
        booking_link_id=str(content.booking_link_id),
        source_url=content.source_url,
        tid=content.tid,
        tracked_url=_tracked_url_for_tid(content.tid),
    )


@router.post("", response_model=ContentResponse, status_code=status.HTTP_201_CREATED)
def create_content(
    payload: ContentCreateRequest,
    current_user: AuthUser = Depends(get_current_auth_user),
    db: Session = Depends(get_db),
) -> ContentResponse:
    booking_link = db.execute(
        _creator_owned_booking_link_query(
            booking_link_id=payload.booking_link_id,
            creator_id=current_user.creator_id,
        )
    ).scalar_one_or_none()
    if booking_link is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="booking link not found",
        )

    content = Content(
        creator_id=current_user.creator_id,
        booking_link_id=booking_link.id,
        source_url=str(payload.source_url),
        tid=uuid.uuid4().hex,
    )
    db.add(content)
    db.commit()
    db.refresh(content)

    logger.info("content_created")

    return _build_content_response(content)
