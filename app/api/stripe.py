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
from app.models.billing_provider import BILLING_CONNECT_STATUS_CONNECTED, BILLING_PROVIDER_STRIPE
from app.models.creator import Creator
from app.schemas.auth import GenericOkResponse
from app.schemas.stripe import StripeConnectStartResponse
from app.services.billing_provider_switch import (
    BILLING_PROVIDER_SWITCH_REASON_SWITCH_TARGET_ALREADY_CONNECTED,
    BillingProviderSwitchError,
    ensure_billing_provider_switch_attempt,
    get_billing_provider_switch_attempt_by_id,
    record_billing_provider_switch_target_connection,
)
from app.services.browser_session import get_browser_session_token, request_prefers_html
from app.services.stripe_connect import build_stripe_connect_state, decode_stripe_connect_state
from app.services.stripe_provider import StripeProvider, StripeProviderError, build_default_stripe_provider

router = APIRouter(prefix="/stripe", tags=["stripe"])
logger = logging.getLogger(__name__)
INVALID_STRIPE_CONNECT_STATE_DETAIL = "invalid stripe connect state"
INVALID_STRIPE_CONNECT_CALLBACK_DETAIL = "invalid stripe connect callback"
STRIPE_CONNECT_INTERRUPTED_STATUS = "stripe-connect-interrupted"
STRIPE_CONNECT_FAILED_STATUS = "stripe-connect-failed"
STRIPE_SWITCH_CONNECTED_STATUS = "billing-provider-switch-connected"
STRIPE_SWITCH_FAILED_STATUS = "billing-provider-switch-failed"


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


def _browser_connect_recovery_redirect(
    *,
    request: Request,
    status_value: str,
) -> RedirectResponse:
    destination = (
        f"/app?status={status_value}"
        if get_browser_session_token(request) is not None
        else f"/sign-in?status={status_value}"
    )
    response = RedirectResponse(url=destination, status_code=status.HTTP_303_SEE_OTHER)
    response.headers["Cache-Control"] = "no-store"
    return response


def _browser_switch_recovery_redirect(*, status_value: str) -> RedirectResponse:
    response = RedirectResponse(
        url=f"/app/account?status={status_value}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


def build_stripe_connect_start_response(
    *,
    request: Request,
    current_user: AuthUser,
    db: Session,
) -> StripeConnectStartResponse:
    creator = current_user.creator
    switch_attempt_id: str | None = None
    if (
        creator.resolved_billing_connect_status == BILLING_CONNECT_STATUS_CONNECTED
        and creator.resolved_billing_provider != BILLING_PROVIDER_STRIPE
    ):
        switch_attempt = ensure_billing_provider_switch_attempt(
            db=db,
            creator=creator,
            target_provider=BILLING_PROVIDER_STRIPE,
        )
        if (
            switch_attempt.target_billing_connect_status == BILLING_CONNECT_STATUS_CONNECTED
            and switch_attempt.target_billing_account_id is not None
        ):
            raise BillingProviderSwitchError(
                reason_code=BILLING_PROVIDER_SWITCH_REASON_SWITCH_TARGET_ALREADY_CONNECTED
            )
        switch_attempt_id = str(switch_attempt.id)
    state = build_stripe_connect_state(
        creator_id=str(current_user.creator_id),
        switch_attempt_id=switch_attempt_id,
    )
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
    db: Session = Depends(get_db),
) -> StripeConnectStartResponse:
    try:
        response = build_stripe_connect_start_response(
            request=request,
            current_user=current_user,
            db=db,
        )
    except BillingProviderSwitchError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=exc.reason_code,
        ) from exc
    db.commit()
    return response


@router.get("/connect/callback", response_model=GenericOkResponse)
def stripe_connect_callback(
    request: Request,
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> GenericOkResponse | RedirectResponse:
    prefers_html = request_prefers_html(request)
    switch_attempt_id: uuid.UUID | None = None

    if error:
        logger.info("stripe_connect_callback_interrupted error=%s", error)
        if prefers_html:
            return _browser_connect_recovery_redirect(
                request=request,
                status_value=STRIPE_CONNECT_INTERRUPTED_STATUS,
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=INVALID_STRIPE_CONNECT_CALLBACK_DETAIL,
        )

    if not state:
        if prefers_html:
            return _browser_connect_recovery_redirect(
                request=request,
                status_value=STRIPE_CONNECT_INTERRUPTED_STATUS,
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=INVALID_STRIPE_CONNECT_STATE_DETAIL,
        )

    try:
        creator = _creator_from_connect_state(db=db, state=state)
        payload = decode_stripe_connect_state(state)
        raw_switch_attempt_id = payload.get("switch_attempt_id")
        if raw_switch_attempt_id is not None:
            switch_attempt_id = uuid.UUID(raw_switch_attempt_id)
    except HTTPException:
        if prefers_html:
            return _browser_connect_recovery_redirect(
                request=request,
                status_value=STRIPE_CONNECT_INTERRUPTED_STATUS,
            )
        raise
    except (JWTError, TypeError, ValueError):
        if prefers_html:
            return _browser_connect_recovery_redirect(
                request=request,
                status_value=STRIPE_CONNECT_INTERRUPTED_STATUS,
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=INVALID_STRIPE_CONNECT_STATE_DETAIL,
        )

    if not code:
        if prefers_html:
            if switch_attempt_id is not None:
                return _browser_switch_recovery_redirect(status_value=STRIPE_SWITCH_FAILED_STATUS)
            return _browser_connect_recovery_redirect(
                request=request,
                status_value=STRIPE_CONNECT_INTERRUPTED_STATUS,
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=INVALID_STRIPE_CONNECT_CALLBACK_DETAIL,
        )

    try:
        stripe_account_id = _stripe_provider(request).exchange_connect_callback(
            code=code,
            state=state,
        )
    except StripeProviderError as exc:
        logger.warning(
            "stripe_connect_callback_provider_error creator_id=%s operation=%s http_status=%s error_code=%s",
            creator.id,
            exc.operation,
            exc.http_status,
            exc.error_code,
        )
        if prefers_html:
            if switch_attempt_id is not None:
                return _browser_switch_recovery_redirect(status_value=STRIPE_SWITCH_FAILED_STATUS)
            return _browser_connect_recovery_redirect(
                request=request,
                status_value=STRIPE_CONNECT_FAILED_STATUS,
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=INVALID_STRIPE_CONNECT_CALLBACK_DETAIL,
        ) from exc

    if not stripe_account_id:
        if prefers_html:
            if switch_attempt_id is not None:
                return _browser_switch_recovery_redirect(status_value=STRIPE_SWITCH_FAILED_STATUS)
            return _browser_connect_recovery_redirect(
                request=request,
                status_value=STRIPE_CONNECT_FAILED_STATUS,
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=INVALID_STRIPE_CONNECT_CALLBACK_DETAIL,
        )

    connected_at = datetime.now(timezone.utc)
    if switch_attempt_id is not None:
        switch_attempt = get_billing_provider_switch_attempt_by_id(
            db=db,
            creator_id=creator.id,
            switch_attempt_id=switch_attempt_id,
        )
        if switch_attempt is None:
            if prefers_html:
                return _browser_switch_recovery_redirect(status_value=STRIPE_SWITCH_FAILED_STATUS)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=INVALID_STRIPE_CONNECT_CALLBACK_DETAIL,
            )
        try:
            record_billing_provider_switch_target_connection(
                db=db,
                creator=creator,
                switch_attempt_id=switch_attempt_id,
                target_provider=BILLING_PROVIDER_STRIPE,
                target_account_id=stripe_account_id,
                connected_at=connected_at,
            )
        except BillingProviderSwitchError:
            if prefers_html:
                return _browser_switch_recovery_redirect(status_value=STRIPE_SWITCH_FAILED_STATUS)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=INVALID_STRIPE_CONNECT_CALLBACK_DETAIL,
            )
        db.commit()
        logger.info("stripe_connect_callback_stored_pending_switch creator_id=%s", creator.id)
        if prefers_html:
            return _browser_switch_recovery_redirect(status_value=STRIPE_SWITCH_CONNECTED_STATUS)
        return GenericOkResponse()

    creator.billing_provider = BILLING_PROVIDER_STRIPE
    creator.billing_connect_status = BILLING_CONNECT_STATUS_CONNECTED
    creator.billing_account_id = stripe_account_id
    creator.billing_connected_at = connected_at
    creator.stripe_account_id = stripe_account_id
    creator.stripe_connect_status = "connected"
    creator.stripe_connected_at = connected_at
    db.add(creator)
    db.commit()
    logger.info("stripe_connect_callback_completed creator_id=%s", creator.id)

    if prefers_html:
        response = RedirectResponse(url="/app", status_code=status.HTTP_303_SEE_OTHER)
        response.headers["Cache-Control"] = "no-store"
        return response

    return GenericOkResponse()
