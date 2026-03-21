from datetime import datetime, timedelta, timezone
from secrets import token_hex

from jose import JWTError, jwt

from app.core.config import get_settings

STRIPE_CONNECT_STATE_PURPOSE = "stripe_connect"


def build_stripe_connect_state(
    *,
    creator_id: str,
    switch_attempt_id: str | None = None,
) -> str:
    settings = get_settings()
    issued_at = datetime.now(timezone.utc)
    payload = {
        "sub": creator_id,
        "purpose": STRIPE_CONNECT_STATE_PURPOSE,
        "jti": token_hex(16),
        "iat": issued_at,
        "exp": issued_at + timedelta(minutes=settings.stripe_connect_state_ttl_minutes),
    }
    if switch_attempt_id is not None:
        payload["switch_attempt_id"] = switch_attempt_id
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_stripe_connect_state(state: str) -> dict:
    settings = get_settings()
    payload = jwt.decode(state, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    if payload.get("purpose") != STRIPE_CONNECT_STATE_PURPOSE:
        raise JWTError("invalid stripe connect state purpose")
    if not payload.get("sub"):
        raise JWTError("missing stripe connect state subject")
    switch_attempt_id = payload.get("switch_attempt_id")
    if switch_attempt_id is not None and not isinstance(switch_attempt_id, str):
        raise JWTError("invalid stripe connect switch attempt id")
    return payload
