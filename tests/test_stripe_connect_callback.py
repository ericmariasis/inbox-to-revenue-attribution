import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy import create_engine, text

from app.core.config import get_settings
from app.main import app
from app.services.stripe_provider import StripeProviderError


def _engine():
    return create_engine(os.environ["TEST_DATABASE_URL"])


def _insert_creator_user(
    *,
    email: str,
    name: str = "Stripe Callback Creator",
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
    def __init__(self, *, account_id: str):
        self.account_id = account_id
        self.onboarding_calls: list[dict[str, str]] = []
        self.exchange_calls: list[dict[str, str]] = []

    def build_connect_onboarding_url(self, *, creator_id: str, state: str) -> str:
        self.onboarding_calls.append({"creator_id": creator_id, "state": state})
        return (
            "https://connect.stripe.com/oauth/authorize"
            f"?response_type=code&client_id=ca_test_story27&state={state}&creator_id={creator_id}"
        )

    def exchange_connect_callback(self, *, code: str, state: str) -> str:
        self.exchange_calls.append({"code": code, "state": state})
        return self.account_id


class _FailingStripeProvider(_StubStripeProvider):
    def exchange_connect_callback(self, *, code: str, state: str) -> str:
        self.exchange_calls.append({"code": code, "state": state})
        raise StripeProviderError(
            "stripe callback exchange failed",
            operation="stripe_connect_callback_exchange",
            http_status=400,
            error_code="invalid_grant",
        )


def test_stripe_connect_callback_persists_connected_fields_and_me_reflects_them():
    inserted = _insert_creator_user(email=f"stripe_callback_{uuid.uuid4().hex}@example.com")
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    provider = _StubStripeProvider(account_id="acct_story27_connected")

    with _override_app_state("stripe_provider", provider):
        with TestClient(app) as client:
            start_response = client.post(
                "/stripe/connect/start",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            state = start_response.json()["state"]
            callback_response = client.get(
                "/stripe/connect/callback",
                params={"code": "auth_code_story27", "state": state},
            )
            me_response = client.get("/me", headers={"Authorization": f"Bearer {access_token}"})

    assert start_response.status_code == 200
    assert callback_response.status_code == 200
    assert callback_response.headers.get("X-Request-Id")
    assert callback_response.json() == {"status": "ok"}
    assert provider.exchange_calls == [{"code": "auth_code_story27", "state": state}]

    with _engine().connect() as conn:
        creator_row = conn.execute(
            text(
                "SELECT stripe_connect_status, stripe_account_id, stripe_connected_at "
                "FROM creators WHERE id = :creator_id"
            ),
            {"creator_id": inserted["creator_id"]},
        ).mappings().one()

    assert creator_row["stripe_connect_status"] == "connected"
    assert creator_row["stripe_account_id"] == provider.account_id
    assert creator_row["stripe_connected_at"] is not None

    assert me_response.status_code == 200
    payload = me_response.json()
    assert payload["id"] == inserted["creator_id"]
    assert payload["stripe_connect_status"] == "connected"
    assert payload["stripe_account_id"] == provider.account_id
    assert payload["stripe_connected_at"] is not None
    assert datetime.fromisoformat(payload["stripe_connected_at"]).astimezone(timezone.utc) == creator_row[
        "stripe_connected_at"
    ].astimezone(timezone.utc)


def test_stripe_connect_callback_rejects_invalid_state_without_mutating_creator():
    inserted = _insert_creator_user(email=f"stripe_invalid_{uuid.uuid4().hex}@example.com")
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    provider = _StubStripeProvider(account_id="acct_should_not_persist")

    with _override_app_state("stripe_provider", provider):
        with TestClient(app) as client:
            start_response = client.post(
                "/stripe/connect/start",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            invalid_state = f"{start_response.json()['state']}tampered"
            callback_response = client.get(
                "/stripe/connect/callback",
                params={"code": "auth_code_invalid", "state": invalid_state},
            )
            me_response = client.get("/me", headers={"Authorization": f"Bearer {access_token}"})

    assert start_response.status_code == 200
    assert callback_response.status_code == 400
    assert callback_response.headers.get("X-Request-Id")
    assert callback_response.json() == {"detail": "invalid stripe connect state"}
    assert provider.exchange_calls == []

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

    assert me_response.status_code == 200
    assert me_response.json()["stripe_connect_status"] == "pending"
    assert me_response.json()["stripe_account_id"] is None
    assert me_response.json()["stripe_connected_at"] is None


def test_stripe_connect_callback_returns_generic_error_when_provider_exchange_fails():
    inserted = _insert_creator_user(email=f"stripe_callback_failure_{uuid.uuid4().hex}@example.com")
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    provider = _FailingStripeProvider(account_id="acct_should_not_persist")

    with _override_app_state("stripe_provider", provider):
        with TestClient(app) as client:
            start_response = client.post(
                "/stripe/connect/start",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            state = start_response.json()["state"]
            callback_response = client.get(
                "/stripe/connect/callback",
                params={"code": "auth_code_provider_failure", "state": state},
            )
            me_response = client.get("/me", headers={"Authorization": f"Bearer {access_token}"})

    assert start_response.status_code == 200
    assert callback_response.status_code == 400
    assert callback_response.headers.get("X-Request-Id")
    assert callback_response.json() == {"detail": "invalid stripe connect callback"}
    assert provider.exchange_calls == [{"code": "auth_code_provider_failure", "state": state}]

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

    assert me_response.status_code == 200
    assert me_response.json()["stripe_connect_status"] == "pending"
    assert me_response.json()["stripe_account_id"] is None
    assert me_response.json()["stripe_connected_at"] is None
