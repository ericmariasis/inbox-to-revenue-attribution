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
    billing_provider: str = "stripe",
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
                "INSERT INTO creators "
                "(id, name, billing_provider, billing_connect_status, billing_account_id, billing_connected_at, "
                "stripe_connect_status, stripe_account_id, stripe_connected_at) "
                "VALUES "
                "(:id, :name, :billing_provider, :billing_connect_status, :billing_account_id, :billing_connected_at, "
                ":stripe_connect_status, :stripe_account_id, :stripe_connected_at)"
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


def _switch_attempt_rows():
    with _engine().connect() as conn:
        return conn.execute(
            text(
                "SELECT creator_id::text AS creator_id, source_billing_provider, target_billing_provider, "
                "target_billing_connect_status, target_billing_account_id, target_billing_connected_at "
                "FROM billing_provider_switch_attempts ORDER BY created_at"
            )
        ).mappings().all()


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
                "SELECT billing_provider, billing_connect_status, billing_account_id, billing_connected_at, "
                "stripe_connect_status, stripe_account_id, stripe_connected_at "
                "FROM creators WHERE id = :creator_id"
            ),
            {"creator_id": inserted["creator_id"]},
        ).mappings().one()

    assert creator_row["billing_provider"] == "stripe"
    assert creator_row["billing_connect_status"] == "connected"
    assert creator_row["billing_account_id"] == provider.account_id
    assert creator_row["billing_connected_at"] is not None
    assert creator_row["stripe_connect_status"] == "connected"
    assert creator_row["stripe_account_id"] == provider.account_id
    assert creator_row["stripe_connected_at"] is not None

    assert me_response.status_code == 200
    payload = me_response.json()
    assert payload["id"] == inserted["creator_id"]
    assert payload["billing_provider"] == "stripe"
    assert payload["billing_connect_status"] == "connected"
    assert payload["billing_account_id"] == provider.account_id
    assert payload["billing_connected_at"] is not None
    assert payload["stripe_connect_status"] == "connected"
    assert payload["stripe_account_id"] == provider.account_id
    assert payload["stripe_connected_at"] is not None
    assert datetime.fromisoformat(payload["billing_connected_at"]).astimezone(timezone.utc) == creator_row[
        "billing_connected_at"
    ].astimezone(timezone.utc)
    assert datetime.fromisoformat(payload["stripe_connected_at"]).astimezone(timezone.utc) == creator_row[
        "stripe_connected_at"
    ].astimezone(timezone.utc)


def test_stripe_connect_callback_stores_pending_switch_attempt_without_mutating_active_provider():
    connected_at = datetime.now(timezone.utc).replace(microsecond=0)
    inserted = _insert_creator_user(
        email=f"stripe_switch_callback_{uuid.uuid4().hex}@example.com",
        stripe_connect_status="pending",
        stripe_account_id=None,
        billing_provider="paypal",
        billing_connect_status="connected",
        billing_account_id="merchant_pp11b_source",
        billing_connected_at=connected_at,
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    provider = _StubStripeProvider(account_id="acct_pp11b_target")

    with _override_app_state("stripe_provider", provider):
        with TestClient(app) as client:
            start_response = client.post(
                "/stripe/connect/start",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            state = start_response.json()["state"]
            callback_response = client.get(
                "/stripe/connect/callback",
                params={"code": "auth_code_pp11b_switch", "state": state},
                headers={"Accept": "text/html,application/xhtml+xml"},
                follow_redirects=False,
            )
            me_response = client.get("/me", headers={"Authorization": f"Bearer {access_token}"})

    assert start_response.status_code == 200
    assert callback_response.status_code == 303
    assert callback_response.headers["location"] == "/app/account?status=billing-provider-switch-connected"
    assert provider.exchange_calls == [{"code": "auth_code_pp11b_switch", "state": state}]

    with _engine().connect() as conn:
        creator_row = conn.execute(
            text(
                "SELECT billing_provider, billing_connect_status, billing_account_id, billing_connected_at, "
                "stripe_connect_status, stripe_account_id, stripe_connected_at "
                "FROM creators WHERE id = :creator_id"
            ),
            {"creator_id": inserted["creator_id"]},
        ).mappings().one()

    switch_attempt_rows = _switch_attempt_rows()
    assert creator_row["billing_provider"] == "paypal"
    assert creator_row["billing_connect_status"] == "connected"
    assert creator_row["billing_account_id"] == "merchant_pp11b_source"
    assert creator_row["stripe_connect_status"] == "pending"
    assert creator_row["stripe_account_id"] is None
    assert len(switch_attempt_rows) == 1
    assert switch_attempt_rows[0]["creator_id"] == inserted["creator_id"]
    assert switch_attempt_rows[0]["source_billing_provider"] == "paypal"
    assert switch_attempt_rows[0]["target_billing_provider"] == "stripe"
    assert switch_attempt_rows[0]["target_billing_connect_status"] == "connected"
    assert switch_attempt_rows[0]["target_billing_account_id"] == "acct_pp11b_target"
    assert switch_attempt_rows[0]["target_billing_connected_at"] is not None
    assert me_response.status_code == 200
    assert me_response.json()["billing_provider"] == "paypal"
    assert me_response.json()["billing_account_id"] == "merchant_pp11b_source"


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
                "SELECT billing_provider, billing_connect_status, billing_account_id, billing_connected_at, "
                "stripe_connect_status, stripe_account_id, stripe_connected_at "
                "FROM creators WHERE id = :creator_id"
            ),
            {"creator_id": inserted["creator_id"]},
        ).mappings().one()

    assert creator_row["billing_provider"] == "stripe"
    assert creator_row["billing_connect_status"] == "pending"
    assert creator_row["billing_account_id"] is None
    assert creator_row["billing_connected_at"] is None
    assert creator_row["stripe_connect_status"] == "pending"
    assert creator_row["stripe_account_id"] is None
    assert creator_row["stripe_connected_at"] is None

    assert me_response.status_code == 200
    assert me_response.json()["billing_provider"] == "stripe"
    assert me_response.json()["billing_connect_status"] == "pending"
    assert me_response.json()["billing_account_id"] is None
    assert me_response.json()["billing_connected_at"] is None
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
                "SELECT billing_provider, billing_connect_status, billing_account_id, billing_connected_at, "
                "stripe_connect_status, stripe_account_id, stripe_connected_at "
                "FROM creators WHERE id = :creator_id"
            ),
            {"creator_id": inserted["creator_id"]},
        ).mappings().one()

    assert creator_row["billing_provider"] == "stripe"
    assert creator_row["billing_connect_status"] == "pending"
    assert creator_row["billing_account_id"] is None
    assert creator_row["billing_connected_at"] is None
    assert creator_row["stripe_connect_status"] == "pending"
    assert creator_row["stripe_account_id"] is None
    assert creator_row["stripe_connected_at"] is None

    assert me_response.status_code == 200
    assert me_response.json()["billing_provider"] == "stripe"
    assert me_response.json()["billing_connect_status"] == "pending"
    assert me_response.json()["billing_account_id"] is None
    assert me_response.json()["billing_connected_at"] is None
    assert me_response.json()["stripe_connect_status"] == "pending"
    assert me_response.json()["stripe_account_id"] is None
    assert me_response.json()["stripe_connected_at"] is None
