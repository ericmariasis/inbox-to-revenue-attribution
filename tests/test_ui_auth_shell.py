import os
import uuid
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy import create_engine, text

from app.core.config import get_settings
from app.main import app
from app.services.email_stub import get_magic_link_outbox

HTML_ACCEPT_HEADERS = {"Accept": "text/html,application/xhtml+xml"}
SESSION_COOKIE_NAME = "ccp_creator_session"


def _engine():
    return create_engine(os.environ["TEST_DATABASE_URL"])


def _latest_magic_link_token_for_email(email: str) -> str:
    for message in reversed(get_magic_link_outbox()):
        if message["email"] == email:
            return message["token"]
    raise AssertionError(f"No magic-link token found for {email}")


def _insert_creator_user(*, email: str, name: str = "UI Creator") -> dict[str, str]:
    creator_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())

    with _engine().begin() as conn:
        conn.execute(
            text("INSERT INTO creators (id, name) VALUES (:id, :name)"),
            {"id": creator_id, "name": name},
        )
        conn.execute(
            text(
                "INSERT INTO auth_users (id, creator_id, email) "
                "VALUES (:id, :creator_id, :email)"
            ),
            {"id": user_id, "creator_id": creator_id, "email": email},
        )

    return {"creator_id": creator_id, "user_id": user_id, "email": email}


def _access_token(*, user_id: str, creator_id: str, email: str, expires_delta: timedelta) -> str:
    settings = get_settings()
    issued_at = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "creator_id": creator_id,
        "email": email,
        "iat": issued_at,
        "exp": issued_at + expires_delta,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def test_sign_in_page_is_browser_accessible():
    with TestClient(app) as client:
        response = client.get("/sign-in", headers=HTML_ACCEPT_HEADERS)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<form" in response.text
    assert 'action="/sign-in"' in response.text
    assert 'name="email"' in response.text
    assert "Creator sign in" in response.text


def test_sign_in_start_redirects_to_confirmation_without_echoing_email():
    email = f"ui_sign_in_{uuid.uuid4().hex}@example.com"

    with TestClient(app) as client:
        response = client.post(
            "/sign-in",
            data={"email": email},
            headers=HTML_ACCEPT_HEADERS,
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/sign-in?status=sent"
    assert email not in response.headers["location"]
    assert _latest_magic_link_token_for_email(email)


def test_app_shell_redirects_unauthenticated_browser_requests():
    with TestClient(app) as client:
        response = client.get("/app", headers=HTML_ACCEPT_HEADERS, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/sign-in"


def test_browser_magic_link_verify_sets_session_cookie_and_lands_in_app_shell():
    email = f"ui_shell_{uuid.uuid4().hex}@example.com"

    with TestClient(app) as client:
        start_response = client.post(
            "/sign-in",
            data={"email": email},
            headers=HTML_ACCEPT_HEADERS,
            follow_redirects=False,
        )
        raw_token = _latest_magic_link_token_for_email(email)

        verify_response = client.get(
            "/auth/magic-link/verify",
            params={"token": raw_token},
            headers=HTML_ACCEPT_HEADERS,
            follow_redirects=False,
        )
        shell_response = client.get("/app", headers=HTML_ACCEPT_HEADERS)

    assert start_response.status_code == 303
    assert verify_response.status_code == 303
    assert verify_response.headers["location"] == "/app"
    assert raw_token not in verify_response.headers["location"]
    assert raw_token not in verify_response.text
    assert f"{SESSION_COOKIE_NAME}=" in verify_response.headers["set-cookie"]
    assert raw_token not in verify_response.headers["set-cookie"]

    assert shell_response.status_code == 200
    assert shell_response.headers["content-type"].startswith("text/html")
    assert "Creator Home" in shell_response.text
    assert email in shell_response.text
    assert raw_token not in shell_response.text


def test_browser_magic_link_verify_failure_redirects_without_echoing_token():
    raw_token = "invalid_browser_magic_link_token"

    with TestClient(app) as client:
        response = client.get(
            "/auth/magic-link/verify",
            params={"token": raw_token},
            headers=HTML_ACCEPT_HEADERS,
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/sign-in?status=invalid-link"
    assert raw_token not in response.headers["location"]
    assert raw_token not in response.text


def test_app_shell_clears_expired_session_cookie():
    inserted = _insert_creator_user(email=f"ui_expired_{uuid.uuid4().hex}@example.com")
    expired_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(minutes=-1),
    )

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, expired_token)
        response = client.get("/app", headers=HTML_ACCEPT_HEADERS, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/sign-in"
    assert f"{SESSION_COOKIE_NAME}=" in response.headers["set-cookie"]
    assert "Max-Age=0" in response.headers["set-cookie"]
