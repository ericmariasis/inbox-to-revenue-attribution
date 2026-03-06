import logging
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import RedirectResponse
from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.booking_link import BookingLink
from app.models.content import Content

router = APIRouter(tags=["redirects"])
logger = logging.getLogger(__name__)


def _redirect_destination_query(*, tid: str) -> Select[tuple[str, str]]:
    return (
        select(BookingLink.calendly_url, Content.tid)
        .join(Content, Content.booking_link_id == BookingLink.id)
        .where(Content.tid == tid)
    )


def _destination_with_canonical_tid(*, destination_url: str, canonical_tid: str) -> str:
    parsed = urlsplit(destination_url)
    query_params = [
        (key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key != "tid"
    ]
    query_params.append(("tid", canonical_tid))

    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode(query_params),
            parsed.fragment,
        )
    )


@router.get("/r/{tid}", status_code=status.HTTP_302_FOUND)
def redirect_by_tid(
    tid: str,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    redirect_row = db.execute(_redirect_destination_query(tid=tid)).one_or_none()
    if redirect_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="link not found",
        )

    destination, canonical_tid = redirect_row
    logger.info("redirect_resolved")

    return RedirectResponse(
        url=_destination_with_canonical_tid(
            destination_url=destination,
            canonical_tid=canonical_tid,
        ),
        status_code=status.HTTP_302_FOUND,
    )
