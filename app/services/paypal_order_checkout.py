from datetime import datetime, timedelta, timezone
from secrets import token_hex
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from jose import JWTError, jwt

from app.core.config import Settings, get_settings

PAYPAL_ORDER_CHECKOUT_STATE_PURPOSE = "paypal_order_checkout"
PAYPAL_ORDER_CHECKOUT_CALLBACK_PATH = "/paypal/orders/callback"


def build_paypal_order_checkout_state(
    *,
    creator_id: str,
    booking_id: str,
    settings: Settings | None = None,
) -> str:
    resolved_settings = settings or get_settings()
    issued_at = datetime.now(timezone.utc)
    payload = {
        "sub": creator_id,
        "booking_id": booking_id,
        "purpose": PAYPAL_ORDER_CHECKOUT_STATE_PURPOSE,
        "jti": token_hex(16),
        "iat": issued_at,
        "exp": issued_at + timedelta(minutes=resolved_settings.paypal_connect_state_ttl_minutes),
    }
    return jwt.encode(payload, resolved_settings.jwt_secret, algorithm=resolved_settings.jwt_algorithm)


def decode_paypal_order_checkout_state(
    state: str,
    *,
    settings: Settings | None = None,
) -> dict:
    resolved_settings = settings or get_settings()
    payload = jwt.decode(state, resolved_settings.jwt_secret, algorithms=[resolved_settings.jwt_algorithm])
    if payload.get("purpose") != PAYPAL_ORDER_CHECKOUT_STATE_PURPOSE:
        raise JWTError("invalid paypal order checkout state purpose")
    if not payload.get("sub"):
        raise JWTError("missing paypal order checkout state subject")
    booking_id = payload.get("booking_id")
    if not isinstance(booking_id, str) or not booking_id:
        raise JWTError("missing paypal order checkout booking id")
    return payload


def build_paypal_order_checkout_return_url(
    *,
    state: str,
    settings: Settings | None = None,
) -> str:
    return _append_query_params(
        _paypal_order_checkout_callback_base_url(settings=settings),
        state=state,
    )


def build_paypal_order_checkout_cancel_url(
    *,
    state: str,
    settings: Settings | None = None,
) -> str:
    return _append_query_params(
        _paypal_order_checkout_callback_base_url(settings=settings),
        state=state,
        cancel="true",
    )


def _paypal_order_checkout_callback_base_url(*, settings: Settings | None = None) -> str:
    resolved_settings = settings or get_settings()
    parsed = urlsplit(resolved_settings.paypal_connect_redirect_uri)
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            PAYPAL_ORDER_CHECKOUT_CALLBACK_PATH,
            "",
            "",
        )
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
