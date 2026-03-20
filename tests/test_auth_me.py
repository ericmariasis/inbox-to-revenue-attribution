import os
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy import create_engine, text

from app.core.config import get_settings
from app.core.request_context import creator_id_ctx
from app.main import app
from app.models.billing_provider import BILLING_PROVIDER_STRIPE


def _engine():
    return create_engine(os.environ["TEST_DATABASE_URL"])


def _insert_creator_user(
    *,
    email: str,
    name: str = "Me Creator",
    stripe_connect_status: str = "pending",
    stripe_account_id: str | None = None,
    stripe_connected_at: datetime | None = None,
    billing_provider: str = BILLING_PROVIDER_STRIPE,
    billing_connect_status: str | None = None,
    billing_account_id: str | None = None,
    billing_connected_at: datetime | None = None,
):
    creator_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    resolved_billing_connect_status = billing_connect_status or stripe_connect_status
    resolved_billing_account_id = billing_account_id if billing_account_id is not None else stripe_account_id
    resolved_billing_connected_at = (
        billing_connected_at if billing_connected_at is not None else stripe_connected_at
    )

    with _engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO creators ("
                "id, name, billing_provider, billing_connect_status, billing_account_id, billing_connected_at, "
                "stripe_connect_status, stripe_account_id, stripe_connected_at"
                ") VALUES ("
                ":id, :name, :billing_provider, :billing_connect_status, :billing_account_id, :billing_connected_at, "
                ":stripe_connect_status, :stripe_account_id, :stripe_connected_at"
                ")"
            ),
            {
                "id": creator_id,
                "name": name,
                "billing_provider": billing_provider,
                "billing_connect_status": resolved_billing_connect_status,
                "billing_account_id": resolved_billing_account_id,
                "billing_connected_at": resolved_billing_connected_at,
                "stripe_connect_status": stripe_connect_status,
                "stripe_account_id": stripe_account_id,
                "stripe_connected_at": stripe_connected_at,
            },
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


def test_me_requires_auth():
    with TestClient(app) as client:
        response = client.get("/me")

    assert response.status_code == 401
    assert response.json() == {"detail": "not authenticated"}


def test_me_rejects_malformed_bearer_token():
    with TestClient(app) as client:
        response = client.get("/me", headers={"Authorization": "Bearer not-a-jwt"})

    assert response.status_code == 401
    assert response.json() == {"detail": "invalid token"}


def test_me_rejects_expired_bearer_token():
    inserted = _insert_creator_user(email=f"expired_{uuid.uuid4().hex}@example.com")
    token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(minutes=-1),
    )

    with TestClient(app) as client:
        response = client.get("/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401
    assert response.json() == {"detail": "invalid token"}


def test_me_returns_creator_profile_for_valid_token():
    connected_at = datetime.now(timezone.utc).replace(microsecond=0)
    inserted = _insert_creator_user(
        email=f"me_{uuid.uuid4().hex}@example.com",
        name="Creator Profile",
        stripe_connect_status="connected",
        stripe_account_id="acct_123",
        stripe_connected_at=connected_at,
    )
    token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )

    captured = {}

    def _capture_log(message: str, *args, **kwargs):
        captured["message"] = message
        captured["creator_id"] = creator_id_ctx.get()

    with patch("app.api.auth.logger.info", side_effect=_capture_log):
        with TestClient(app) as client:
            response = client.get("/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.headers.get("X-Request-Id")
    payload = response.json()
    assert payload == {
        "id": inserted["creator_id"],
        "email": inserted["email"],
        "name": "Creator Profile",
        "billing_provider": "stripe",
        "billing_connect_status": "connected",
        "billing_account_id": "acct_123",
        "billing_connected_at": payload["billing_connected_at"],
        "stripe_connect_status": "connected",
        "stripe_account_id": "acct_123",
        "stripe_connected_at": payload["stripe_connected_at"],
    }
    assert datetime.fromisoformat(payload["billing_connected_at"]).astimezone(timezone.utc) == connected_at
    assert datetime.fromisoformat(payload["stripe_connected_at"]).astimezone(timezone.utc) == connected_at

    assert captured["message"] == "me_retrieved"
    assert captured["creator_id"] == inserted["creator_id"]
