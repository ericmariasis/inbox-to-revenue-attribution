import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy import create_engine, text

from app.core.config import get_settings
from app.main import app
from app.services.stripe_connect import decode_stripe_connect_state


def _engine():
    return create_engine(os.environ["TEST_DATABASE_URL"])


def _insert_creator_user(
    *,
    email: str,
    name: str = "Stripe Creator",
    stripe_connect_status: str = "pending",
    stripe_account_id: str | None = None,
    stripe_connected_at: datetime | None = None,
):
    creator_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())

    with _engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO creators (id, name, stripe_connect_status, stripe_account_id, stripe_connected_at) "
                "VALUES (:id, :name, :stripe_connect_status, :stripe_account_id, :stripe_connected_at)"
            ),
            {
                "id": creator_id,
                "name": name,
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


@contextmanager
def _override_app_state(name, value):
    had_attr = hasattr(app.state, name)
    previous_value = getattr(app.state, name, None)
    setattr(app.state, name, value)
    try:
        yield
    finally:
        if had_attr:
            setattr(app.state, name, previous_value)
        else:
            delattr(app.state, name)


class _StubStripeProvider:
    def __init__(self):
        self.calls: list[dict[str, str]] = []

    def build_connect_onboarding_url(self, *, creator_id: str, state: str) -> str:
        self.calls.append({"creator_id": creator_id, "state": state})
        return (
            "https://connect.stripe.com/oauth/authorize"
            f"?response_type=code&client_id=ca_test_story26&state={state}&creator_id={creator_id}"
        )

    def exchange_connect_callback(self, *, code: str, state: str) -> str:
        raise AssertionError(f"unexpected callback exchange code={code} state={state}")


def test_stripe_connect_start_returns_provider_url_and_app_issued_state():
    inserted = _insert_creator_user(email=f"stripe_{uuid.uuid4().hex}@example.com")
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    provider = _StubStripeProvider()

    with _override_app_state("stripe_provider", provider):
        with TestClient(app) as client:
            response = client.post(
                "/stripe/connect/start",
                headers={"Authorization": f"Bearer {access_token}"},
            )

    assert response.status_code == 200
    assert response.headers.get("X-Request-Id")
    payload = response.json()
    decoded_state = decode_stripe_connect_state(payload["state"])
    assert payload["state"]
    assert len(payload["state"]) > 20
    assert payload["onboarding_url"].startswith("https://connect.stripe.com/oauth/authorize")
    assert f"state={payload['state']}" in payload["onboarding_url"]
    assert decoded_state["sub"] == inserted["creator_id"]
    assert decoded_state["purpose"] == "stripe_connect"
    assert provider.calls == [
        {
            "creator_id": inserted["creator_id"],
            "state": payload["state"],
        }
    ]

    with _engine().connect() as conn:
        creator_row = conn.execute(
            text(
                "SELECT stripe_connect_status, stripe_account_id, stripe_connected_at "
                "FROM creators WHERE id = :creator_id"
            ),
            {"creator_id": inserted["creator_id"]},
        ).mappings().one()

    assert creator_row["stripe_connect_status"] == "pending"
    assert creator_row["stripe_account_id"] is None
    assert creator_row["stripe_connected_at"] is None


def test_stripe_connect_start_requires_auth():
    with TestClient(app) as client:
        response = client.post("/stripe/connect/start")

    assert response.status_code == 401
    assert response.json() == {"detail": "not authenticated"}
