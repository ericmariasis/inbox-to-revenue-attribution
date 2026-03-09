from fastapi import Request
from starlette.responses import Response

from app.core.config import Settings

SESSION_COOKIE_NAME = "ccp_creator_session"


def request_prefers_html(request: Request) -> bool:
    accept_header = request.headers.get("accept", "").lower()
    return "text/html" in accept_header or "application/xhtml+xml" in accept_header


def get_browser_session_token(request: Request) -> str | None:
    return request.cookies.get(SESSION_COOKIE_NAME)


def set_browser_session_cookie(response: Response, access_token: str, *, settings: Settings) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=access_token,
        httponly=True,
        max_age=settings.jwt_access_token_ttl_hours * 3600,
        path="/",
        samesite="lax",
        secure=_secure_cookie_enabled(settings),
    )


def clear_browser_session_cookie(response: Response, *, settings: Settings) -> None:
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/",
        httponly=True,
        samesite="lax",
        secure=_secure_cookie_enabled(settings),
    )


def _secure_cookie_enabled(settings: Settings) -> bool:
    return not settings.is_local_env()
