import logging

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import RedirectResponse
from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.booking_link import BookingLink
from app.models.content import Content

router = APIRouter(tags=["redirects"])
logger = logging.getLogger(__name__)


def _redirect_destination_query(*, tid: str) -> Select[tuple[str]]:
    return (
        select(BookingLink.calendly_url)
        .join(Content, Content.booking_link_id == BookingLink.id)
        .where(Content.tid == tid)
    )


@router.get("/r/{tid}", status_code=status.HTTP_302_FOUND)
def redirect_by_tid(
    tid: str,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    destination = db.execute(_redirect_destination_query(tid=tid)).scalar_one_or_none()
    if destination is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="link not found",
        )

    logger.info("redirect_resolved")

    return RedirectResponse(
        url=destination,
        status_code=status.HTTP_302_FOUND,
    )
