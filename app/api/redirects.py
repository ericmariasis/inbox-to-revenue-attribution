import logging
import re
from datetime import datetime, timedelta, timezone
from secrets import token_hex
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import get_db
from app.models.booking_link import BookingLink
from app.models.content import Content
from app.services.click_events import DEFAULT_CLICK_EVENT_PUBLISHER, ClickEventPublisher, build_click_event

router = APIRouter(tags=["redirects"])
logger = logging.getLogger(__name__)

REDIRECT_SESSION_COOKIE_NAME = "ccp_sid"
REDIRECT_SESSION_COOKIE_PATH = "/r"
REDIRECT_SESSION_COOKIE_TTL = timedelta(days=14)
REDIRECT_SESSION_COOKIE_TTL_SECONDS = int(REDIRECT_SESSION_COOKIE_TTL.total_seconds())
_REDIRECT_SESSION_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


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


def _redirect_session_cookie_secure(*, app_env: str) -> bool:
    return app_env.lower() in {"production", "prod", "staging"}


def _redirect_session_id(*, existing_session_id: str | None) -> str:
    if existing_session_id and _REDIRECT_SESSION_ID_PATTERN.fullmatch(existing_session_id):
        return existing_session_id

    return token_hex(16)


def _set_redirect_session_cookie(
    response: RedirectResponse,
    *,
    session_id: str,
    app_env: str,
) -> None:
    response.set_cookie(
        key=REDIRECT_SESSION_COOKIE_NAME,
        value=session_id,
        max_age=REDIRECT_SESSION_COOKIE_TTL_SECONDS,
        expires=datetime.now(timezone.utc) + REDIRECT_SESSION_COOKIE_TTL,
        path=REDIRECT_SESSION_COOKIE_PATH,
        secure=_redirect_session_cookie_secure(app_env=app_env),
        httponly=True,
        samesite="lax",
    )


def _click_event_publisher(request: Request) -> ClickEventPublisher:
    return getattr(request.app.state, "click_event_publisher", DEFAULT_CLICK_EVENT_PUBLISHER)


def _request_client_ip(request: Request) -> str | None:
    if request.client is None:
        return None

    return request.client.host


@router.get("/r/{tid}", status_code=status.HTTP_302_FOUND)
def redirect_by_tid(
    tid: str,
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    redirect_row = db.execute(_redirect_destination_query(tid=tid)).one_or_none()
    if redirect_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="link not found",
        )

    destination, canonical_tid = redirect_row
    settings = getattr(request.app.state, "settings", None) or get_settings()
    session_id = _redirect_session_id(
        existing_session_id=request.cookies.get(REDIRECT_SESSION_COOKIE_NAME)
    )
    logger.info("redirect_resolved")

    response = RedirectResponse(
        url=_destination_with_canonical_tid(
            destination_url=destination,
            canonical_tid=canonical_tid,
        ),
        status_code=status.HTTP_302_FOUND,
    )
    _set_redirect_session_cookie(
        response,
        session_id=session_id,
        app_env=settings.app_env,
    )

    click_event = build_click_event(
        tid=canonical_tid,
        session_id=session_id,
        ip_address=_request_client_ip(request),
    )
    try:
        _click_event_publisher(request).publish(click_event)
    except Exception as exc:
        logger.warning(
            "click_event_publish_failed tid=%s event_id=%s error=%s",
            canonical_tid,
            click_event.event_id,
            type(exc).__name__,
        )

    return response
