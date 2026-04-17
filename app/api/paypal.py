import html
import json
import logging
import uuid
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from jose import JWTError
from sqlalchemy.orm import Session

from app.api.deps import (
    browser_auth_user_is_allowlisted_operator,
    get_current_auth_user,
    get_optional_browser_auth_user,
)
from app.core.config import Settings, get_settings
from app.db.session import SessionLocal
from app.db.session import get_db
from app.models.auth_user import AuthUser
from app.models.invoice import Invoice
from app.models.billing_provider import (
    BILLING_CONNECT_STATUS_CONNECTED,
    BILLING_PROVIDER_PAYPAL,
)
from app.models.creator import Creator
from app.schemas.auth import GenericOkResponse
from app.schemas.paypal import (
    PayPalConnectStartResponse,
    PayPalOrderCaptureRequest,
    PayPalOrderCaptureResponse,
    PayPalOrderStartRequest,
    PayPalOrderStartResponse,
)
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
from app.services.paypal_order_checkout import decode_paypal_order_checkout_state
from app.services.paypal_orders import (
    PAYPAL_ORDER_FLOW_REASON_PROVIDER_ERROR,
    PayPalOrderFlowError,
    PayPalOrdersService,
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
INVALID_PAYPAL_ORDER_CALLBACK_DETAIL = "invalid paypal order callback"
PAYPAL_ORDER_START_UNAVAILABLE_DETAIL = "paypal order start unavailable"
PAYPAL_ORDER_START_NOT_FOUND_DETAIL = "paypal order start not found"
PAYPAL_ORDER_START_NOT_FOUND_PAGE_TITLE = "PayPal payment could not be started"
PAYPAL_ORDER_CHECKOUT_PAGE_NOT_FOUND_DETAIL = "paypal checkout page not found"


def _settings(request: Request) -> Settings:
    return getattr(request.app.state, "settings", get_settings())


def _paypal_provider(request: Request) -> PayPalProvider:
    return getattr(request.app.state, "paypal_provider", build_default_paypal_provider(settings=_settings(request)))


def _paypal_orders_service(request: Request) -> PayPalOrdersService:
    return PayPalOrdersService(
        session_factory=SessionLocal,
        provider=_paypal_provider(request),
        settings=_settings(request),
    )


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


def _query_param_is_explicit_true(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"true", "1", "yes"}


def _browser_sign_in_redirect() -> RedirectResponse:
    response = RedirectResponse(url="/sign-in", status_code=status.HTTP_303_SEE_OTHER)
    response.headers["Cache-Control"] = "no-store"
    return response


def _allowlisted_operator_browser_user(
    *,
    request: Request,
    current_user: AuthUser | None,
) -> AuthUser | RedirectResponse:
    if current_user is None:
        return _browser_sign_in_redirect()
    if not browser_auth_user_is_allowlisted_operator(
        current_user,
        settings=_settings(request),
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=PAYPAL_ORDER_CHECKOUT_PAGE_NOT_FOUND_DETAIL,
        )
    return current_user


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


def _browser_order_failure_response() -> HTMLResponse:
    return _connect_result_page(
        title="PayPal payment could not be completed",
        body="This PayPal payment return could not be verified. Start the order again if you still need approval and capture.",
        status_code=status.HTTP_400_BAD_REQUEST,
    )


def _browser_order_canceled_response() -> HTMLResponse:
    return _connect_result_page(
        title="PayPal payment was not completed",
        body="The buyer left PayPal before approving the payment. The local PayPal order remains open until you restart or reuse the approval link.",
        status_code=status.HTTP_200_OK,
    )


def _browser_order_checkout_page_error_response(*, body: str) -> HTMLResponse:
    return _connect_result_page(
        title="PayPal checkout page could not be prepared",
        body=body,
        status_code=status.HTTP_400_BAD_REQUEST,
    )


def _paypal_javascript_sdk_src(
    *,
    settings: Settings,
    merchant_id: str,
    currency: str,
) -> str:
    query_params = {
        "client-id": settings.selected_paypal_client_id(),
        "merchant-id": merchant_id,
        "currency": currency,
        "intent": "capture",
        "commit": "true",
        "components": "buttons",
    }
    if settings.paypal_environment_value() == "sandbox":
        query_params["buyer-country"] = "US"
    return "https://www.paypal.com/sdk/js?" + urlencode(query_params)


def _render_paypal_order_checkout_page(
    *,
    settings: Settings,
    current_user: AuthUser,
    booking_id: uuid.UUID,
    invoice: Invoice,
) -> HTMLResponse:
    merchant_id = invoice.resolved_provider_account_id or ""
    currency = (invoice.currency or "USD").upper()
    amount_dollars = f"{invoice.amount_cents / 100:.2f}"
    sdk_src = _paypal_javascript_sdk_src(
        settings=settings,
        merchant_id=merchant_id,
        currency=currency,
    )
    partner_attribution_id = html.escape(settings.paypal_partner_attribution_id)
    creator_email = html.escape(current_user.email)
    creator_name = html.escape(current_user.creator.name)
    environment_label = html.escape(settings.paypal_environment_value().title())
    body = f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>PayPal Checkout Proof</title>
    <style>
      :root {{
        color-scheme: light;
        --bg: #f7f0e4;
        --panel: #fffaf2;
        --ink: #2b2118;
        --muted: #6e5c4d;
        --accent: #b85c38;
        --line: #dccbb8;
        --success: #24543a;
        --error: #8d2d1f;
      }}
      * {{ box-sizing: border-box; }}
      body {{
        margin: 0;
        font-family: Georgia, "Times New Roman", serif;
        background:
          radial-gradient(circle at top, rgba(184, 92, 56, 0.12), transparent 32%),
          var(--bg);
        color: var(--ink);
      }}
      main {{
        max-width: 920px;
        margin: 0 auto;
        padding: 40px 20px 72px;
      }}
      .stack > * + * {{ margin-top: 14px; }}
      .hero h1 {{
        margin: 0;
        font-size: clamp(2rem, 5vw, 3.5rem);
        line-height: 1;
      }}
      .hero p {{
        margin: 0;
        max-width: 52rem;
        color: var(--muted);
        font-size: 1.05rem;
      }}
      .eyebrow {{
        margin: 0 0 8px;
        font-size: 0.72rem;
        letter-spacing: 0.14em;
        text-transform: uppercase;
        color: var(--accent);
        font-weight: 700;
      }}
      .grid {{
        display: grid;
        gap: 18px;
      }}
      @media (min-width: 780px) {{
        .grid {{
          grid-template-columns: 1.2fr 0.8fr;
        }}
      }}
      .card {{
        background: var(--panel);
        border: 1px solid var(--line);
        border-radius: 20px;
        padding: 20px;
        box-shadow: 0 16px 30px rgba(76, 50, 32, 0.08);
      }}
      .meta {{
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
        gap: 12px;
      }}
      .meta strong {{
        display: block;
        font-size: 0.72rem;
        letter-spacing: 0.08em;
        text-transform: uppercase;
        color: var(--accent);
        margin-bottom: 4px;
      }}
      .status {{
        min-height: 4rem;
        padding: 14px 16px;
        border-radius: 14px;
        background: #f3e8d8;
        border: 1px solid var(--line);
        color: var(--muted);
      }}
      .status.success {{
        color: var(--success);
        border-color: rgba(36, 84, 58, 0.3);
        background: rgba(36, 84, 58, 0.08);
      }}
      .status.error {{
        color: var(--error);
        border-color: rgba(141, 45, 31, 0.3);
        background: rgba(141, 45, 31, 0.08);
      }}
      .pill {{
        display: inline-block;
        padding: 6px 10px;
        border-radius: 999px;
        background: rgba(184, 92, 56, 0.12);
        color: var(--accent);
        font-size: 0.82rem;
        font-weight: 700;
      }}
      a {{
        color: var(--accent);
      }}
      #paypal-buttons {{
        min-height: 52px;
      }}
      code {{
        font-family: Consolas, monospace;
        word-break: break-word;
      }}
    </style>
    <script src="{html.escape(sdk_src)}" data-partner-attribution-id="{partner_attribution_id}"></script>
  </head>
  <body>
    <main class="stack">
      <section class="hero stack">
        <p class="eyebrow">Operator-gated proof</p>
        <h1>PayPal checkout proof</h1>
        <p>Use the documented JavaScript SDK buyer path for one existing booking. This page starts from a real server-created order and keeps final capture on the server so local paid truth stays canonical.</p>
      </section>
      <section class="grid">
        <article class="card stack">
          <div>
            <p class="eyebrow">Buyer approval</p>
            <h2>Approve and capture one PayPal order</h2>
          </div>
          <div id="status" class="status" role="status" aria-live="polite">Ready. Click the PayPal button to continue the buyer approval flow.</div>
          <div id="paypal-buttons"></div>
        </article>
        <aside class="card stack">
          <div>
            <p class="eyebrow">Current proof context</p>
            <span class="pill">{environment_label} environment</span>
          </div>
          <div class="meta">
            <div><strong>Creator</strong>{creator_name}</div>
            <div><strong>Signed in as</strong>{creator_email}</div>
            <div><strong>Merchant ID</strong><code>{html.escape(merchant_id)}</code></div>
            <div><strong>Booking ID</strong><code>{html.escape(str(booking_id))}</code></div>
            <div><strong>Invoice ID</strong><code>{html.escape(str(invoice.id))}</code></div>
            <div><strong>Order ID</strong><code>{html.escape(invoice.resolved_provider_invoice_id or "")}</code></div>
            <div><strong>Amount</strong>{html.escape(currency)} {html.escape(amount_dollars)}</div>
          </div>
          <p><a href="/app/account">Return to account</a></p>
        </aside>
      </section>
      <script>
        const bookingId = {json.dumps(str(booking_id))};
        const orderId = {json.dumps(invoice.resolved_provider_invoice_id or "")};
        const statusNode = document.getElementById("status");

        function setStatus(message, cssClass) {{
          statusNode.textContent = message;
          statusNode.className = "status" + (cssClass ? " " + cssClass : "");
        }}

        if (!window.paypal) {{
          setStatus("PayPal JavaScript SDK did not load on this page.", "error");
        }} else {{
          window.paypal.Buttons({{
            createOrder() {{
              return orderId;
            }},
            onApprove(data) {{
              setStatus("Buyer approved the order. Capturing server-side now.", "");
              return fetch("/paypal/orders/capture", {{
                method: "POST",
                headers: {{
                  "Content-Type": "application/json",
                  "Accept": "application/json",
                }},
                credentials: "same-origin",
                body: JSON.stringify({{
                  booking_id: bookingId,
                  provider_order_id: data.orderID,
                }}),
              }})
                .then(async (response) => {{
                  const payload = await response.json().catch(() => ({{}}));
                  if (!response.ok) {{
                    const detail = payload.detail || "Capture failed.";
                    throw new Error(detail);
                  }}
                  return payload;
                }})
                .then((payload) => {{
                  const captureId = payload.capture_id || "capture recorded";
                  setStatus("Payment captured. Capture ID: " + captureId + ".", "success");
                }})
                .catch((error) => {{
                  setStatus(error.message || "Capture failed after buyer approval.", "error");
                  throw error;
                }});
            }},
            onCancel() {{
              setStatus("Buyer canceled before approval.", "error");
            }},
            onError(error) {{
              const message = error && error.message ? error.message : "PayPal checkout failed before approval.";
              setStatus(message, "error");
            }},
          }}).render("#paypal-buttons");
        }}
      </script>
  </body>
</html>
"""
    response = HTMLResponse(body, status_code=status.HTTP_200_OK)
    response.headers["Cache-Control"] = "no-store"
    return response


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


@router.post("/orders/start", response_model=PayPalOrderStartResponse)
def paypal_order_start(
    request: Request,
    payload: PayPalOrderStartRequest,
    current_user: AuthUser = Depends(get_current_auth_user),
) -> PayPalOrderStartResponse:
    if not _paypal_available_to_current_user(
        request=request,
        current_user=current_user,
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=PAYPAL_CONNECT_NOT_FOUND_DETAIL,
        )

    service = _paypal_orders_service(request)
    try:
        result = service.start_order(
            creator_id=current_user.creator_id,
            booking_id=payload.booking_id,
        )
    except PayPalOrderFlowError as exc:
        if exc.reason_code == PAYPAL_ORDER_FLOW_REASON_PROVIDER_ERROR:
            logger.warning(
                "paypal_order_start_provider_error creator_id=%s booking_id=%s operation=%s http_status=%s error_code=%s",
                current_user.creator_id,
                payload.booking_id,
                exc.provider_operation,
                exc.provider_http_status,
                exc.provider_error_code,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=PAYPAL_ORDER_START_UNAVAILABLE_DETAIL,
            ) from exc
        if exc.reason_code == "booking_not_found":
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=PAYPAL_ORDER_START_NOT_FOUND_DETAIL,
            ) from exc
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.reason_code,
        ) from exc

    return PayPalOrderStartResponse(
        invoice_id=result.invoice_id,
        provider_order_id=result.provider_order_id,
        approval_url=result.approval_url,
        state=result.state,
    )


@router.get(
    "/orders/checkout-page",
    response_class=HTMLResponse,
    response_model=None,
)
def paypal_order_checkout_page(
    request: Request,
    booking_id: uuid.UUID = Query(...),
    current_user: AuthUser | None = Depends(get_optional_browser_auth_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    browser_user = _allowlisted_operator_browser_user(
        request=request,
        current_user=current_user,
    )
    if isinstance(browser_user, RedirectResponse):
        return browser_user

    service = _paypal_orders_service(request)
    try:
        result = service.start_order(
            creator_id=browser_user.creator_id,
            booking_id=booking_id,
        )
    except PayPalOrderFlowError as exc:
        if exc.reason_code == PAYPAL_ORDER_FLOW_REASON_PROVIDER_ERROR:
            logger.warning(
                "paypal_order_checkout_page_provider_error creator_id=%s booking_id=%s operation=%s http_status=%s error_code=%s",
                browser_user.creator_id,
                booking_id,
                exc.provider_operation,
                exc.provider_http_status,
                exc.provider_error_code,
            )
        return _browser_order_checkout_page_error_response(
            body=(
                "The PayPal checkout page could not be prepared for this booking. "
                f"Current reason: {exc.reason_code}."
            )
        )

    invoice = db.get(Invoice, result.invoice_id)
    if invoice is None or invoice.creator_id != browser_user.creator_id:
        return _browser_order_checkout_page_error_response(
            body="The PayPal checkout page was prepared, but the local invoice record could not be loaded afterward."
        )
    if (
        browser_user.creator.resolved_billing_provider != BILLING_PROVIDER_PAYPAL
        or browser_user.creator.billing_account_id is None
    ):
        return _browser_order_checkout_page_error_response(
            body="This creator does not currently have a connected PayPal seller identity for the JavaScript SDK proof."
        )

    return _render_paypal_order_checkout_page(
        settings=_settings(request),
        current_user=browser_user,
        booking_id=booking_id,
        invoice=invoice,
    )


@router.post("/orders/capture", response_model=PayPalOrderCaptureResponse)
def paypal_order_capture(
    request: Request,
    payload: PayPalOrderCaptureRequest,
    current_user: AuthUser | None = Depends(get_optional_browser_auth_user),
) -> PayPalOrderCaptureResponse:
    browser_user = _allowlisted_operator_browser_user(
        request=request,
        current_user=current_user,
    )
    if isinstance(browser_user, RedirectResponse):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="not authenticated",
        )

    service = _paypal_orders_service(request)
    try:
        result = service.capture_order(
            creator_id=browser_user.creator_id,
            booking_id=payload.booking_id,
            provider_order_id=payload.provider_order_id,
        )
    except PayPalOrderFlowError as exc:
        if exc.reason_code == PAYPAL_ORDER_FLOW_REASON_PROVIDER_ERROR:
            logger.warning(
                "paypal_order_capture_browser_provider_error creator_id=%s booking_id=%s operation=%s http_status=%s error_code=%s",
                browser_user.creator_id,
                payload.booking_id,
                exc.provider_operation,
                exc.provider_http_status,
                exc.provider_error_code,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=PAYPAL_ORDER_START_UNAVAILABLE_DETAIL,
            ) from exc
        raise HTTPException(
            status_code=exc.status_code,
            detail=INVALID_PAYPAL_ORDER_CALLBACK_DETAIL,
        ) from exc

    return PayPalOrderCaptureResponse(
        outcome=result.outcome,
        invoice_id=result.invoice_id,
        provider_order_id=result.provider_order_id,
        capture_id=result.capture_id,
        paid_at=result.paid_at,
    )


@router.get("/orders/callback", response_model=GenericOkResponse)
def paypal_order_callback(
    request: Request,
    state: str | None = Query(default=None),
    token: str | None = Query(default=None),
    cancel: str | None = Query(default=None),
) -> GenericOkResponse | HTMLResponse:
    prefers_html = request_prefers_html(request)
    if not state:
        if prefers_html:
            return _browser_order_failure_response()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=INVALID_PAYPAL_ORDER_CALLBACK_DETAIL,
        )

    try:
        decoded_state = decode_paypal_order_checkout_state(
            state,
            settings=_settings(request),
        )
        creator_id = uuid.UUID(decoded_state["sub"])
        booking_id = uuid.UUID(decoded_state["booking_id"])
    except (JWTError, KeyError, TypeError, ValueError):
        if prefers_html:
            return _browser_order_failure_response()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=INVALID_PAYPAL_ORDER_CALLBACK_DETAIL,
        )

    if _query_param_is_explicit_true(cancel):
        return _browser_order_canceled_response()

    if token is None:
        if prefers_html:
            return _browser_order_failure_response()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=INVALID_PAYPAL_ORDER_CALLBACK_DETAIL,
        )

    service = _paypal_orders_service(request)
    try:
        result = service.capture_order(
            creator_id=creator_id,
            booking_id=booking_id,
            provider_order_id=token,
        )
    except PayPalOrderFlowError as exc:
        if exc.reason_code == PAYPAL_ORDER_FLOW_REASON_PROVIDER_ERROR:
            logger.warning(
                "paypal_order_capture_provider_error creator_id=%s booking_id=%s operation=%s http_status=%s error_code=%s",
                creator_id,
                booking_id,
                exc.provider_operation,
                exc.provider_http_status,
                exc.provider_error_code,
            )
        if prefers_html:
            return _browser_order_failure_response()
        raise HTTPException(
            status_code=exc.status_code,
            detail=(
                PAYPAL_ORDER_START_UNAVAILABLE_DETAIL
                if exc.reason_code == PAYPAL_ORDER_FLOW_REASON_PROVIDER_ERROR
                else INVALID_PAYPAL_ORDER_CALLBACK_DETAIL
            ),
        ) from exc

    if prefers_html:
        return _connect_result_page(
            title="PayPal payment completed",
            body=(
                "The PayPal order was captured server-side and the local invoice was marked paid. "
                f"Order ID: {result.provider_order_id}."
            ),
            status_code=status.HTTP_200_OK,
        )

    return GenericOkResponse()
