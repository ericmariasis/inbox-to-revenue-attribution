import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from jose import JWTError
from sqlalchemy.orm import Session

from app.api.deps import get_current_auth_user
from app.db.session import get_db
from app.models.auth_user import AuthUser
from app.models.creator import Creator
from app.schemas.auth import GenericOkResponse
from app.schemas.stripe import StripeConnectStartResponse
from app.services.browser_session import request_prefers_html
from app.services.stripe_connect import build_stripe_connect_state, decode_stripe_connect_state
from app.services.stripe_provider import StripeProvider, StripeProviderError, build_default_stripe_provider

router = APIRouter(prefix="/stripe", tags=["stripe"])
logger = logging.getLogger(__name__)
INVALID_STRIPE_CONNECT_STATE_DETAIL = "invalid stripe connect state"
INVALID_STRIPE_CONNECT_CALLBACK_DETAIL = "invalid stripe connect callback"


def _stripe_provider(request: Request) -> StripeProvider:
    return getattr(request.app.state, "stripe_provider", build_default_stripe_provider())


def _creator_from_connect_state(*, db: Session, state: str) -> Creator:
    try:
        payload = decode_stripe_connect_state(state)
        creator_id = uuid.UUID(payload["sub"])
    except (JWTError, KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=INVALID_STRIPE_CONNECT_STATE_DETAIL,
        ) from exc

    creator = db.get(Creator, creator_id)
    if creator is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=INVALID_STRIPE_CONNECT_STATE_DETAIL,
        )
    return creator


def build_stripe_connect_start_response(
    *,
    request: Request,
    current_user: AuthUser,
) -> StripeConnectStartResponse:
    state = build_stripe_connect_state(creator_id=str(current_user.creator_id))
    onboarding_url = _stripe_provider(request).build_connect_onboarding_url(
        creator_id=str(current_user.creator_id),
        state=state,
    )
    logger.info("stripe_connect_start_created")
    return StripeConnectStartResponse(
        onboarding_url=onboarding_url,
        state=state,
    )


@router.post("/connect/start", response_model=StripeConnectStartResponse)
def stripe_connect_start(
    request: Request,
    current_user: AuthUser = Depends(get_current_auth_user),
) -> StripeConnectStartResponse:
    return build_stripe_connect_start_response(
        request=request,
        current_user=current_user,
    )


@router.get("/connect/callback", response_model=GenericOkResponse)
def stripe_connect_callback(
    request: Request,
    code: str = Query(..., min_length=1),
    state: str = Query(..., min_length=1),
    db: Session = Depends(get_db),
) -> GenericOkResponse | RedirectResponse:
    creator = _creator_from_connect_state(db=db, state=state)

    try:
        stripe_account_id = _stripe_provider(request).exchange_connect_callback(
            code=code,
            state=state,
        )
    except StripeProviderError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=INVALID_STRIPE_CONNECT_CALLBACK_DETAIL,
        ) from exc

    if not stripe_account_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=INVALID_STRIPE_CONNECT_CALLBACK_DETAIL,
        )

    creator.stripe_account_id = stripe_account_id
    creator.stripe_connect_status = "connected"
    creator.stripe_connected_at = datetime.now(timezone.utc)
    db.add(creator)
    db.commit()
    logger.info("stripe_connect_callback_completed creator_id=%s", creator.id)

    if request_prefers_html(request):
        response = RedirectResponse(url="/app", status_code=status.HTTP_303_SEE_OTHER)
        response.headers["Cache-Control"] = "no-store"
        return response

    return GenericOkResponse()
