import html
import logging
import uuid
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from jose import JWTError
from sqlalchemy.orm import Session

from app.api.deps import get_current_auth_user
from app.core.config import Settings, get_settings
from app.db.session import get_db
from app.models.auth_user import AuthUser
from app.models.billing_provider import (
    BILLING_CONNECT_STATUS_CONNECTED,
    BILLING_PROVIDER_PAYPAL,
)
from app.models.creator import Creator
from app.schemas.auth import GenericOkResponse
from app.schemas.paypal import PayPalConnectStartResponse
from app.services.billing_provider_switch import (
    BILLING_PROVIDER_SWITCH_REASON_SWITCH_TARGET_ALREADY_CONNECTED,
    BillingProviderSwitchError,
    ensure_billing_provider_switch_attempt,
    get_billing_provider_switch_attempt_by_id,
    record_billing_provider_switch_target_connection,
)
from app.services.browser_session import request_prefers_html
from app.services.paypal_connect import (
    build_paypal_connect_state,
    build_paypal_tracking_id,
    decode_paypal_connect_state,
)
from app.services.paypal_provider import (
    PayPalProvider,
    PayPalProviderError,
    PayPalSellerStatus,
    build_default_paypal_provider,
)

router = APIRouter(prefix="/paypal", tags=["paypal"])
logger = logging.getLogger(__name__)
INVALID_PAYPAL_CONNECT_STATE_DETAIL = "invalid paypal connect state"
INVALID_PAYPAL_CONNECT_CALLBACK_DETAIL = "invalid paypal connect callback"
PAYPAL_CONNECT_UNAVAILABLE_DETAIL = "paypal connect unavailable"
PAYPAL_CONNECT_ALREADY_CONNECTED_DETAIL = "billing provider already connected"
PAYPAL_CONNECT_NOT_FOUND_DETAIL = "paypal connect not found"


def _settings(request: Request) -> Settings:
    return getattr(request.app.state, "settings", get_settings())


def _paypal_provider(request: Request) -> PayPalProvider:
    return getattr(request.app.state, "paypal_provider", build_default_paypal_provider(settings=_settings(request)))


def _append_query_params(url: str, **params: str) -> str:
    parsed = urlsplit(url)
    existing_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    merged_pairs = [(key, value) for key, value in existing_pairs if key not in params]
    merged_pairs.extend((key, value) for key, value in params.items())
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode(merged_pairs),
            parsed.fragment,
        )
    )


def _creator_from_connect_state(*, db: Session, state: str, settings: Settings) -> tuple[Creator, dict]:
    try:
        payload = decode_paypal_connect_state(state, settings=settings)
        creator_id = uuid.UUID(payload["sub"])
    except (JWTError, KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=INVALID_PAYPAL_CONNECT_STATE_DETAIL,
        ) from exc

    creator = db.get(Creator, creator_id)
    if creator is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=INVALID_PAYPAL_CONNECT_STATE_DETAIL,
        )
    return creator, payload


def _creator_can_start_paypal_onboarding(*, creator: Creator) -> bool:
    if creator.resolved_billing_connect_status != BILLING_CONNECT_STATUS_CONNECTED:
        return True
    return creator.resolved_billing_provider != BILLING_PROVIDER_PAYPAL


def _paypal_available_to_current_user(*, request: Request, current_user: AuthUser) -> bool:
    return _settings(request).paypal_available_to_creator(current_user.email)


def build_paypal_connect_start_response(
    *,
    request: Request,
    current_user: AuthUser,
    db: Session,
) -> PayPalConnectStartResponse:
    creator = current_user.creator
    if not _creator_can_start_paypal_onboarding(creator=creator):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=PAYPAL_CONNECT_ALREADY_CONNECTED_DETAIL,
        )

    settings = _settings(request)
    switch_attempt_id: str | None = None
    tracking_id = build_paypal_tracking_id()
    if (
        creator.resolved_billing_connect_status == BILLING_CONNECT_STATUS_CONNECTED
        and creator.resolved_billing_provider != BILLING_PROVIDER_PAYPAL
    ):
        switch_attempt = ensure_billing_provider_switch_attempt(
            db=db,
            creator=creator,
            target_provider=BILLING_PROVIDER_PAYPAL,
        )
        if (
            switch_attempt.target_billing_connect_status == BILLING_CONNECT_STATUS_CONNECTED
            and switch_attempt.target_billing_account_id is not None
        ):
            raise BillingProviderSwitchError(
                reason_code=BILLING_PROVIDER_SWITCH_REASON_SWITCH_TARGET_ALREADY_CONNECTED
            )
        switch_attempt_id = str(switch_attempt.id)
        tracking_id = switch_attempt.target_billing_provider_correlation_id or build_paypal_tracking_id()
    state = build_paypal_connect_state(
        creator_id=str(current_user.creator_id),
        tracking_id=tracking_id,
        switch_attempt_id=switch_attempt_id,
        settings=settings,
    )
    return_url = _append_query_params(settings.paypal_connect_redirect_uri, state=state)
    try:
        onboarding = _paypal_provider(request).create_connect_onboarding(
            tracking_id=tracking_id,
            return_url=return_url,
        )
    except PayPalProviderError as exc:
        logger.warning(
            "paypal_connect_start_provider_error creator_id=%s operation=%s http_status=%s error_code=%s",
            creator.id,
            exc.operation,
            exc.http_status,
            exc.error_code,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=PAYPAL_CONNECT_UNAVAILABLE_DETAIL,
        ) from exc

    logger.info("paypal_connect_start_created")
    return PayPalConnectStartResponse(
        onboarding_url=onboarding.onboarding_url,
        state=state,
    )


def _connect_result_page(*, title: str, body: str, status_code: int) -> HTMLResponse:
    response = HTMLResponse(
        f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <title>{html.escape(title)}</title>
  </head>
  <body>
    <main>
      <h1>{html.escape(title)}</h1>
      <p>{html.escape(body)}</p>
      <p><a href="/app/account">Return to account</a></p>
    </main>
  </body>
</html>
""",
        status_code=status_code,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


def _browser_connect_failure_response() -> HTMLResponse:
    return _connect_result_page(
        title="PayPal setup could not be completed",
        body="This PayPal onboarding return could not be verified. Start the setup again if you still need to connect PayPal.",
        status_code=status.HTTP_400_BAD_REQUEST,
    )


def _query_param_is_explicit_false(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"false", "0", "no"}


def _callback_indicates_denied_permissions(
    *,
    permissions_granted: str | None,
    consent_status: str | None,
) -> bool:
    return _query_param_is_explicit_false(permissions_granted) or _query_param_is_explicit_false(consent_status)


def _verified_callback_matches_state(
    *,
    expected_tracking_id: str,
    callback_tracking_id: str | None,
    callback_merchant_id: str | None,
    verified_status: PayPalSellerStatus,
) -> bool:
    if callback_tracking_id is not None and callback_tracking_id != expected_tracking_id:
        return False
    if verified_status.tracking_id != expected_tracking_id:
        return False
    if callback_merchant_id is not None and callback_merchant_id != verified_status.merchant_id:
        return False
    return verified_status.payments_receivable and verified_status.primary_email_confirmed


@router.post("/connect/start", response_model=PayPalConnectStartResponse)
def paypal_connect_start(
    request: Request,
    current_user: AuthUser = Depends(get_current_auth_user),
    db: Session = Depends(get_db),
) -> PayPalConnectStartResponse:
    if not _paypal_available_to_current_user(
        request=request,
        current_user=current_user,
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=PAYPAL_CONNECT_NOT_FOUND_DETAIL,
        )
    try:
        response = build_paypal_connect_start_response(
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
def paypal_connect_callback(
    request: Request,
    state: str | None = Query(default=None),
    merchantId: str | None = Query(default=None),
    merchantIdInPayPal: str | None = Query(default=None),
    permissionsGranted: str | None = Query(default=None),
    consentStatus: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> GenericOkResponse | HTMLResponse:
    prefers_html = request_prefers_html(request)
    settings = _settings(request)
    switch_attempt_id: uuid.UUID | None = None

    if not state or _callback_indicates_denied_permissions(
        permissions_granted=permissionsGranted,
        consent_status=consentStatus,
    ):
        if prefers_html:
            return _browser_connect_failure_response()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=INVALID_PAYPAL_CONNECT_CALLBACK_DETAIL,
        )

    try:
        creator, payload = _creator_from_connect_state(db=db, state=state, settings=settings)
    except HTTPException:
        if prefers_html:
            return _browser_connect_failure_response()
        raise

    expected_tracking_id = payload["tracking_id"]
    raw_switch_attempt_id = payload.get("switch_attempt_id")
    if raw_switch_attempt_id is not None:
        try:
            switch_attempt_id = uuid.UUID(raw_switch_attempt_id)
        except (TypeError, ValueError):
            if prefers_html:
                return _browser_connect_failure_response()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=INVALID_PAYPAL_CONNECT_CALLBACK_DETAIL,
            )

    if (
        switch_attempt_id is None
        and creator.resolved_billing_connect_status == BILLING_CONNECT_STATUS_CONNECTED
    ):
        if creator.resolved_billing_provider != BILLING_PROVIDER_PAYPAL:
            if prefers_html:
                return _browser_connect_failure_response()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=INVALID_PAYPAL_CONNECT_CALLBACK_DETAIL,
            )
        if (
            creator.billing_account_id == merchantIdInPayPal
            and creator.billing_provider_correlation_id == expected_tracking_id
        ):
            if prefers_html:
                return _connect_result_page(
                    title="PayPal setup completed",
                    body="This PayPal seller is already connected for billing.",
                    status_code=status.HTTP_200_OK,
                )
            return GenericOkResponse()

    try:
        verified_status = _paypal_provider(request).get_verified_seller_status(
            tracking_id=expected_tracking_id,
        )
    except PayPalProviderError as exc:
        logger.warning(
            "paypal_connect_callback_provider_error creator_id=%s operation=%s http_status=%s error_code=%s",
            creator.id,
            exc.operation,
            exc.http_status,
            exc.error_code,
        )
        if prefers_html:
            return _browser_connect_failure_response()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=INVALID_PAYPAL_CONNECT_CALLBACK_DETAIL,
        ) from exc

    if not _verified_callback_matches_state(
        expected_tracking_id=expected_tracking_id,
        callback_tracking_id=merchantId,
        callback_merchant_id=merchantIdInPayPal,
        verified_status=verified_status,
    ):
        if prefers_html:
            return _browser_connect_failure_response()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=INVALID_PAYPAL_CONNECT_CALLBACK_DETAIL,
        )

    if (
        switch_attempt_id is None
        and creator.resolved_billing_connect_status == BILLING_CONNECT_STATUS_CONNECTED
    ):
        if creator.billing_account_id != verified_status.merchant_id:
            if prefers_html:
                return _browser_connect_failure_response()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=INVALID_PAYPAL_CONNECT_CALLBACK_DETAIL,
            )
        if (
            creator.billing_provider_correlation_id is not None
            and creator.billing_provider_correlation_id != verified_status.tracking_id
        ):
            if prefers_html:
                return _browser_connect_failure_response()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=INVALID_PAYPAL_CONNECT_CALLBACK_DETAIL,
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
                return _browser_connect_failure_response()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=INVALID_PAYPAL_CONNECT_CALLBACK_DETAIL,
            )
        try:
            record_billing_provider_switch_target_connection(
                db=db,
                creator=creator,
                switch_attempt_id=switch_attempt_id,
                target_provider=BILLING_PROVIDER_PAYPAL,
                target_account_id=verified_status.merchant_id,
                connected_at=connected_at,
                target_provider_correlation_id=verified_status.tracking_id,
            )
        except BillingProviderSwitchError:
            if prefers_html:
                return _browser_connect_failure_response()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=INVALID_PAYPAL_CONNECT_CALLBACK_DETAIL,
            )
        db.commit()
        logger.info("paypal_connect_callback_stored_pending_switch creator_id=%s", creator.id)
        if prefers_html:
            return _connect_result_page(
                title="PayPal switch setup completed",
                body=(
                    "The PayPal seller was verified server-side and saved as the pending replacement "
                    f"provider. Merchant ID: {verified_status.merchant_id}."
                ),
                status_code=status.HTTP_200_OK,
            )
        return GenericOkResponse()

    creator.billing_provider = BILLING_PROVIDER_PAYPAL
    creator.billing_connect_status = BILLING_CONNECT_STATUS_CONNECTED
    creator.billing_account_id = verified_status.merchant_id
    creator.billing_connected_at = connected_at
    creator.billing_provider_correlation_id = verified_status.tracking_id
    db.add(creator)
    db.commit()
    logger.info("paypal_connect_callback_completed creator_id=%s", creator.id)

    if prefers_html:
        return _connect_result_page(
            title="PayPal setup completed",
            body=(
                "The PayPal seller was verified server-side and connected for billing. "
                f"Merchant ID: {verified_status.merchant_id}."
            ),
            status_code=status.HTTP_200_OK,
        )

    return GenericOkResponse()
