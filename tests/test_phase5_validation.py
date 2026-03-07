import hashlib
import hmac
import json
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from time import time

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.main import app
from app.models.creator import Creator
from app.services.email_stub import get_magic_link_outbox
from app.services.stripe_account_readiness import creator_has_billable_stripe_account
from app.services.stripe_connect import decode_stripe_connect_state
from app.services.stripe_provider import StripeAccountReadiness


def _engine():
    return create_engine(os.environ["TEST_DATABASE_URL"])


def _latest_magic_link_token_for_email(email: str) -> str:
    outbox = get_magic_link_outbox()
    for message in reversed(outbox):
        if message["email"] == email:
            return message["token"]

    raise AssertionError(f"No magic-link token captured for {email}")


def _stripe_signature_header(*, payload: bytes, secret: str, timestamp: int | None = None) -> str:
    resolved_timestamp = timestamp or int(time())
    signed_payload = f"{resolved_timestamp}.".encode("utf-8") + payload
    signature = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return f"t={resolved_timestamp},v1={signature}"


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
    def __init__(self, *, account_id: str, readiness: StripeAccountReadiness):
        self.account_id = account_id
        self.readiness = readiness
        self.onboarding_calls: list[dict[str, str]] = []
        self.exchange_calls: list[dict[str, str]] = []
        self.readiness_calls: list[str] = []

    def build_connect_onboarding_url(self, *, creator_id: str, state: str) -> str:
        self.onboarding_calls.append({"creator_id": creator_id, "state": state})
        return (
            "https://connect.stripe.com/oauth/authorize"
            f"?response_type=code&client_id=ca_test_story30&state={state}&creator_id={creator_id}"
        )

    def exchange_connect_callback(self, *, code: str, state: str) -> str:
        self.exchange_calls.append({"code": code, "state": state})
        return self.account_id

    def get_account_readiness(self, *, stripe_account_id: str) -> StripeAccountReadiness:
        self.readiness_calls.append(stripe_account_id)
        return self.readiness


class _CaptureStripeWebhookRouter:
    def __init__(self):
        self.events: list[dict[str, str | None]] = []

    def handle_event(self, *, event) -> None:
        self.events.append(
            {
                "stripe_event_id": event.stripe_event_id,
                "stripe_account_id": event.stripe_account_id,
                "event_type": event.event_type,
            }
        )


def test_phase5_stripe_connect_flow_end_to_end():
    creator_email = f"phase5_creator_{uuid.uuid4().hex}@example.com"
    provider = _StubStripeProvider(
        account_id="acct_story30_connected",
        readiness=StripeAccountReadiness(charges_enabled=True),
    )
    webhook_router = _CaptureStripeWebhookRouter()
    webhook_secret = get_settings().stripe_webhook_secret
    webhook_payload = json.dumps(
        {
            "id": "evt_story30_valid",
            "type": "invoice.payment_succeeded",
            "account": provider.account_id,
            "data": {"object": {"id": "in_story30_valid"}},
        }
    ).encode("utf-8")
    webhook_signature = _stripe_signature_header(
        payload=webhook_payload,
        secret=webhook_secret,
    )

    with _override_app_state("stripe_provider", provider):
        with _override_app_state("stripe_webhook_router", webhook_router):
            with TestClient(app) as client:
                me_without_auth_response = client.get("/me")
                start_response = client.post(
                    "/auth/magic-link/start",
                    json={"email": creator_email},
                )
                verify_response = client.get(
                    "/auth/magic-link/verify",
                    params={"token": _latest_magic_link_token_for_email(creator_email)},
                )
                access_token = verify_response.json()["access_token"]
                connect_start_response = client.post(
                    "/stripe/connect/start",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                connect_start_payload = connect_start_response.json()
                callback_response = client.get(
                    "/stripe/connect/callback",
                    params={"code": "auth_code_story30", "state": connect_start_payload["state"]},
                )
                me_response = client.get("/me", headers={"Authorization": f"Bearer {access_token}"})
                webhook_response = client.post(
                    "/webhooks/stripe",
                    content=webhook_payload,
                    headers={
                        "Content-Type": "application/json",
                        "Stripe-Signature": webhook_signature,
                    },
                )

    decoded_state = decode_stripe_connect_state(connect_start_payload["state"])
    creator_id = uuid.UUID(decoded_state["sub"])

    with Session(_engine()) as db:
        creator = db.execute(select(Creator).where(Creator.id == creator_id)).scalar_one()
        persisted_connected_at = creator.stripe_connected_at
        persisted_account_id = creator.stripe_account_id
        is_billable = creator_has_billable_stripe_account(creator=creator, provider=provider)

    assert me_without_auth_response.status_code == 401
    assert me_without_auth_response.headers.get("X-Request-Id")
    assert me_without_auth_response.json() == {"detail": "not authenticated"}

    assert start_response.status_code == 200
    assert start_response.headers.get("X-Request-Id")
    assert start_response.json() == {"status": "ok"}

    assert verify_response.status_code == 200
    assert verify_response.headers.get("X-Request-Id")
    assert verify_response.json()["token_type"] == "bearer"

    assert connect_start_response.status_code == 200
    assert connect_start_response.headers.get("X-Request-Id")
    assert connect_start_payload["state"]
    assert connect_start_payload["onboarding_url"].startswith("https://connect.stripe.com/oauth/authorize")
    assert f"state={connect_start_payload['state']}" in connect_start_payload["onboarding_url"]
    assert decoded_state["sub"] == str(creator_id)
    assert decoded_state["purpose"] == "stripe_connect"
    assert provider.onboarding_calls == [{"creator_id": str(creator_id), "state": connect_start_payload["state"]}]

    assert callback_response.status_code == 200
    assert callback_response.headers.get("X-Request-Id")
    assert callback_response.json() == {"status": "ok"}
    assert provider.exchange_calls == [{"code": "auth_code_story30", "state": connect_start_payload["state"]}]

    assert me_response.status_code == 200
    assert me_response.headers.get("X-Request-Id")
    assert me_response.json()["id"] == str(creator_id)
    assert me_response.json()["email"] == creator_email
    assert me_response.json()["stripe_connect_status"] == "connected"
    assert me_response.json()["stripe_account_id"] == provider.account_id
    assert me_response.json()["stripe_connected_at"] is not None

    assert persisted_account_id == provider.account_id
    assert persisted_connected_at is not None
    assert (
        datetime.fromisoformat(me_response.json()["stripe_connected_at"]).astimezone(timezone.utc)
        == persisted_connected_at.astimezone(timezone.utc)
    )

    assert is_billable is True
    assert provider.readiness_calls == [provider.account_id]

    assert webhook_response.status_code == 200
    assert webhook_response.headers.get("X-Request-Id")
    assert webhook_response.json() == {"status": "ok"}
    assert webhook_router.events == [
        {
            "stripe_event_id": "evt_story30_valid",
            "stripe_account_id": provider.account_id,
            "event_type": "invoice.payment_succeeded",
        }
    ]

    with Session(_engine()) as db:
        creator_after_webhook = db.execute(select(Creator).where(Creator.id == creator_id)).scalar_one()

    assert creator_after_webhook.stripe_connect_status == "connected"
    assert creator_after_webhook.stripe_account_id == provider.account_id
    assert creator_after_webhook.stripe_connected_at is not None
