import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy import create_engine, text

from app.core.config import get_settings
from app.main import app
from app.services.paypal_connect import decode_paypal_connect_state
from app.services.paypal_provider import PayPalConnectOnboardingResult


def _engine():
    return create_engine(os.environ["TEST_DATABASE_URL"])


def _insert_creator_user(
    *,
    email: str,
    name: str = "PayPal Creator",
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


class _StubPayPalProvider:
    def __init__(self):
        self.start_calls: list[dict[str, str]] = []

    def create_connect_onboarding(
        self,
        *,
        tracking_id: str,
        return_url: str,
    ) -> PayPalConnectOnboardingResult:
        self.start_calls.append(
            {
                "tracking_id": tracking_id,
                "return_url": return_url,
            }
        )
        return PayPalConnectOnboardingResult(
            onboarding_url=(
                "https://www.sandbox.paypal.com/bizsignup/partner/entry"
                f"?tracking_id={tracking_id}"
            ),
            tracking_id=tracking_id,
        )

    def get_verified_seller_status(self, *, tracking_id: str):
        raise AssertionError(f"unexpected seller lookup tracking_id={tracking_id}")


def test_paypal_connect_start_returns_provider_url_and_app_issued_state():
    inserted = _insert_creator_user(email=f"paypal_start_{uuid.uuid4().hex}@example.com")
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    provider = _StubPayPalProvider()

    with _override_app_state("paypal_provider", provider):
        with TestClient(app) as client:
            response = client.post(
                "/paypal/connect/start",
                headers={"Authorization": f"Bearer {access_token}"},
            )

    assert response.status_code == 200
    assert response.headers.get("X-Request-Id")
    payload = response.json()
    decoded_state = decode_paypal_connect_state(payload["state"])
    callback_query = parse_qs(urlparse(provider.start_calls[0]["return_url"]).query)

    assert payload["state"]
    assert len(payload["state"]) > 20
    assert payload["onboarding_url"].startswith("https://www.sandbox.paypal.com/bizsignup/partner/entry")
    assert decoded_state["sub"] == inserted["creator_id"]
    assert decoded_state["tracking_id"].startswith("ccp-paypal-")
    assert callback_query["state"] == [payload["state"]]
    assert provider.start_calls == [
        {
            "tracking_id": decoded_state["tracking_id"],
            "return_url": provider.start_calls[0]["return_url"],
        }
    ]

    with _engine().connect() as conn:
        creator_row = conn.execute(
            text(
                "SELECT billing_provider, billing_connect_status, billing_account_id, "
                "billing_provider_correlation_id "
                "FROM creators WHERE id = :creator_id"
            ),
            {"creator_id": inserted["creator_id"]},
        ).mappings().one()

    assert creator_row["billing_provider"] == "stripe"
    assert creator_row["billing_connect_status"] == "pending"
    assert creator_row["billing_account_id"] is None
    assert creator_row["billing_provider_correlation_id"] is None


def test_paypal_connect_start_requires_auth():
    with TestClient(app) as client:
        response = client.post("/paypal/connect/start")

    assert response.status_code == 401
    assert response.json() == {"detail": "not authenticated"}


def test_paypal_connect_start_rejects_creators_with_existing_connected_provider():
    connected_at = datetime.now(timezone.utc)
    inserted = _insert_creator_user(
        email=f"paypal_connected_{uuid.uuid4().hex}@example.com",
        stripe_connect_status="connected",
        stripe_account_id="acct_existing_connected",
        stripe_connected_at=connected_at,
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    provider = _StubPayPalProvider()

    with _override_app_state("paypal_provider", provider):
        with TestClient(app) as client:
            response = client.post(
                "/paypal/connect/start",
                headers={"Authorization": f"Bearer {access_token}"},
            )

    assert response.status_code == 409
    assert response.json() == {"detail": "billing provider already connected"}
    assert provider.start_calls == []
