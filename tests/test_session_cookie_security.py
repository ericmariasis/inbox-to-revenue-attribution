from http.cookies import SimpleCookie

from fastapi.responses import RedirectResponse
from starlette.responses import Response

from app.api.redirects import _set_redirect_session_cookie
from app.core.config import Settings
from app.services.browser_session import set_browser_session_cookie


def _cookie_from_header(cookie_header: str, name: str):
    parsed = SimpleCookie()
    parsed.load(cookie_header)
    return parsed[name]


def test_browser_session_cookie_is_http_only_lax_and_local_http_compatible():
    response = Response()

    set_browser_session_cookie(
        response,
        "access-token",
        settings=Settings.model_validate({"app_env": "local"}),
    )

    cookie = _cookie_from_header(response.headers["set-cookie"], "ccp_creator_session")

    assert cookie["httponly"]
    assert cookie["path"] == "/"
    assert cookie["samesite"].lower() == "lax"
    assert not cookie["secure"]


def test_browser_session_cookie_is_secure_in_non_local_envs():
    response = Response()

    set_browser_session_cookie(
        response,
        "access-token",
        settings=Settings.model_validate({"app_env": "preview"}),
    )

    cookie = _cookie_from_header(response.headers["set-cookie"], "ccp_creator_session")

    assert cookie["secure"]


def test_redirect_session_cookie_is_secure_in_non_local_envs():
    response = RedirectResponse(url="https://calendly.com/example/story55-cookie-check")

    _set_redirect_session_cookie(
        response,
        session_id="a" * 32,
        app_env="preview",
    )

    cookie = _cookie_from_header(response.headers["set-cookie"], "ccp_sid")

    assert cookie["httponly"]
    assert cookie["path"] == "/r"
    assert cookie["samesite"].lower() == "lax"
    assert cookie["secure"]
