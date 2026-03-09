import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.api.deps import get_current_auth_user
from app.core.config import get_settings
from app.db.session import get_db
from app.models.auth_user import AuthUser
from app.schemas.auth import AccessTokenResponse, GenericOkResponse, MeResponse, MagicLinkStartRequest
from app.services.auth_magic_link import (
    START_RETRY_DETAIL,
    VERIFY_FAILURE_DETAIL,
    start_magic_link,
    verify_magic_link_token,
)
from app.services.browser_session import (
    clear_browser_session_cookie,
    request_prefers_html,
    set_browser_session_cookie,
)
from app.services.email_provider import MagicLinkEmailDeliveryError

router = APIRouter(prefix="/auth", tags=["auth"])
me_router = APIRouter(tags=["auth"])
logger = logging.getLogger(__name__)


@router.post("/magic-link/start", response_model=GenericOkResponse)
def magic_link_start(
    request: Request,
    payload: MagicLinkStartRequest,
    db: Session = Depends(get_db),
) -> GenericOkResponse:
    try:
        start_magic_link(
            db,
            payload.email,
            provider=request.app.state.email_provider,
        )
    except MagicLinkEmailDeliveryError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=START_RETRY_DETAIL,
        ) from exc
    return GenericOkResponse()


@router.get("/magic-link/verify", response_model=AccessTokenResponse)
def magic_link_verify(
    request: Request,
    token: str = Query(..., min_length=1),
    db: Session = Depends(get_db),
) -> AccessTokenResponse | RedirectResponse:
    settings = get_settings()

    try:
        access_token = verify_magic_link_token(db, token)
    except ValueError as exc:
        if request_prefers_html(request):
            response = RedirectResponse(
                url="/sign-in?status=invalid-link",
                status_code=status.HTTP_303_SEE_OTHER,
            )
            clear_browser_session_cookie(response, settings=settings)
            response.headers["Cache-Control"] = "no-store"
            return response

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=VERIFY_FAILURE_DETAIL,
        ) from exc

    if request_prefers_html(request):
        response = RedirectResponse(
            url="/app",
            status_code=status.HTTP_303_SEE_OTHER,
        )
        set_browser_session_cookie(response, access_token, settings=settings)
        response.headers["Cache-Control"] = "no-store"
        return response

    return AccessTokenResponse(access_token=access_token)


@me_router.get("/me", response_model=MeResponse)
def get_me(current_user: AuthUser = Depends(get_current_auth_user)) -> MeResponse:
    creator = current_user.creator
    logger.info("me_retrieved")
    return MeResponse(
        id=str(creator.id),
        email=current_user.email,
        name=creator.name,
        stripe_connect_status=creator.stripe_connect_status,
        stripe_account_id=creator.stripe_account_id,
        stripe_connected_at=creator.stripe_connected_at,
    )
