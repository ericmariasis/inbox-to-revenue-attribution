from datetime import datetime, timedelta, timezone
from secrets import token_hex

from jose import JWTError, jwt

from app.core.config import Settings, get_settings

PAYPAL_CONNECT_STATE_PURPOSE = "paypal_connect"


def build_paypal_tracking_id() -> str:
    issued_at = int(datetime.now(timezone.utc).timestamp())
    return f"ccp-paypal-{issued_at}-{token_hex(4)}"


def build_paypal_connect_state(
    *,
    creator_id: str,
    tracking_id: str,
    switch_attempt_id: str | None = None,
    settings: Settings | None = None,
) -> str:
    resolved_settings = settings or get_settings()
    issued_at = datetime.now(timezone.utc)
    payload = {
        "sub": creator_id,
        "tracking_id": tracking_id,
        "purpose": PAYPAL_CONNECT_STATE_PURPOSE,
        "jti": token_hex(16),
        "iat": issued_at,
        "exp": issued_at + timedelta(minutes=resolved_settings.paypal_connect_state_ttl_minutes),
    }
    if switch_attempt_id is not None:
        payload["switch_attempt_id"] = switch_attempt_id
    return jwt.encode(payload, resolved_settings.jwt_secret, algorithm=resolved_settings.jwt_algorithm)


def decode_paypal_connect_state(
    state: str,
    *,
    settings: Settings | None = None,
) -> dict:
    resolved_settings = settings or get_settings()
    payload = jwt.decode(state, resolved_settings.jwt_secret, algorithms=[resolved_settings.jwt_algorithm])
    if payload.get("purpose") != PAYPAL_CONNECT_STATE_PURPOSE:
        raise JWTError("invalid paypal connect state purpose")
    if not payload.get("sub"):
        raise JWTError("missing paypal connect state subject")
    tracking_id = payload.get("tracking_id")
    if not isinstance(tracking_id, str) or not tracking_id:
        raise JWTError("missing paypal connect state tracking id")
    switch_attempt_id = payload.get("switch_attempt_id")
    if switch_attempt_id is not None and not isinstance(switch_attempt_id, str):
        raise JWTError("invalid paypal connect switch attempt id")
    return payload
