import logging
import re
from datetime import datetime, timedelta, timezone
from secrets import token_hex
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.core.config import get_settings, is_local_app_env
from app.db.session import get_db
from app.models.booking_link import BookingLink
from app.models.booking_provider import (
    BOOKING_PROVIDER_CALENDLY,
    BOOKING_PROVIDER_FULLSCOPE,
)
from app.models.content import Content
from app.services.click_events import DEFAULT_CLICK_EVENT_PUBLISHER, ClickEventPublisher, build_click_event
from app.services.rate_limit import (
    DEFAULT_SHARED_RATE_LIMITER,
    REDIRECT_SOFT_LIMIT_POLICY,
    RateLimitPolicy,
    SharedRateLimiter,
    build_redirect_rate_limit_bucket_key,
)

router = APIRouter(tags=["redirects"])
logger = logging.getLogger(__name__)

REDIRECT_SESSION_COOKIE_NAME = "ccp_sid"
REDIRECT_SESSION_COOKIE_PATH = "/r"
REDIRECT_SESSION_COOKIE_TTL = timedelta(days=14)
REDIRECT_SESSION_COOKIE_TTL_SECONDS = int(REDIRECT_SESSION_COOKIE_TTL.total_seconds())
_REDIRECT_SESSION_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_CALENDLY_TRACKING_QUERY_PARAM = "utm_content"
_FULLSCOPE_TRACKING_QUERY_PARAM = "ccp_attribution_tid"
_LEGACY_TID_QUERY_PARAM = "tid"


def _redirect_destination_query(*, tid: str) -> Select[tuple[str, str]]:
    return (
        select(
            BookingLink.provider,
            BookingLink.destination_url,
            BookingLink.calendly_url,
            Content.tid,
        )
        .join(Content, Content.booking_link_id == BookingLink.id)
        .where(Content.tid == tid)
    )


def _destination_with_query_param(
    *,
    destination_url: str,
    query_param_name: str,
    canonical_tid: str,
    stale_query_param_names: set[str],
) -> str:
    parsed = urlsplit(destination_url)
    query_params = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key not in stale_query_param_names
    ]
    query_params.append((query_param_name, canonical_tid))

    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode(query_params),
            parsed.fragment,
        )
    )


def _destination_with_canonical_tid(*, provider: str, destination_url: str, canonical_tid: str) -> str:
    if provider == BOOKING_PROVIDER_CALENDLY:
        return _destination_with_query_param(
            destination_url=destination_url,
            query_param_name=_CALENDLY_TRACKING_QUERY_PARAM,
            canonical_tid=canonical_tid,
            stale_query_param_names={
                _CALENDLY_TRACKING_QUERY_PARAM,
                _LEGACY_TID_QUERY_PARAM,
            },
        )

    if provider == BOOKING_PROVIDER_FULLSCOPE:
        return _destination_with_query_param(
            destination_url=destination_url,
            query_param_name=_FULLSCOPE_TRACKING_QUERY_PARAM,
            canonical_tid=canonical_tid,
            stale_query_param_names={
                _FULLSCOPE_TRACKING_QUERY_PARAM,
                _LEGACY_TID_QUERY_PARAM,
            },
        )

    return destination_url


def _redirect_session_cookie_secure(*, app_env: str) -> bool:
    return not is_local_app_env(app_env)


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


def _shared_rate_limiter(request: Request) -> SharedRateLimiter:
    return getattr(request.app.state, "shared_rate_limiter", DEFAULT_SHARED_RATE_LIMITER)


def _redirect_soft_limit_policy(request: Request) -> RateLimitPolicy:
    return getattr(request.app.state, "redirect_soft_limit_policy", REDIRECT_SOFT_LIMIT_POLICY)


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
        logger.info("redirect_lookup_not_found tid=%s", tid)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="link not found",
        )

    provider, destination, legacy_calendly_url, canonical_tid = redirect_row
    resolved_destination = destination or legacy_calendly_url
    if resolved_destination is None:
        logger.warning("redirect_lookup_missing_destination tid=%s provider=%s", tid, provider)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="link not found",
        )

    settings = getattr(request.app.state, "settings", None) or get_settings()
    session_id = _redirect_session_id(
        existing_session_id=request.cookies.get(REDIRECT_SESSION_COOKIE_NAME)
    )

    response = RedirectResponse(
        url=_destination_with_canonical_tid(
            provider=provider,
            destination_url=resolved_destination,
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
    redirect_soft_limit_policy = _redirect_soft_limit_policy(request)
    rate_limit_state = _shared_rate_limiter(request).record_attempt(
        policy=redirect_soft_limit_policy,
        bucket_key=build_redirect_rate_limit_bucket_key(
            hashed_ip=click_event.hashed_ip,
            tid=canonical_tid,
        ),
    )
    logger.info(
        "redirect_resolved tid=%s click_event_id=%s soft_limited=%s attempt_count=%s limit=%s",
        canonical_tid,
        click_event.event_id,
        rate_limit_state.soft_limited,
        rate_limit_state.attempt_count,
        rate_limit_state.limit,
    )
    if rate_limit_state.soft_limited:
        logger.info(
            "redirect_rate_limited namespace=%s tid=%s hashed_ip=%s attempt_count=%s limit=%s window_seconds=%s",
            redirect_soft_limit_policy.namespace,
            canonical_tid,
            click_event.hashed_ip,
            rate_limit_state.attempt_count,
            rate_limit_state.limit,
            rate_limit_state.window_seconds,
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
