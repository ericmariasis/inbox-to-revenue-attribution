from jose import JWTError, jwt

from app.core.config import get_settings


def decode_access_token(token: str) -> dict:
    settings = get_settings()
    return jwt.decode(
        token,
        settings.jwt_secret,
        algorithms=[settings.jwt_algorithm],
    )


def decode_access_token_or_none(token: str) -> dict | None:
    try:
        return decode_access_token(token)
    except JWTError:
        return None
