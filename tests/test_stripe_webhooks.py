import hashlib
import hmac
import json
from contextlib import contextmanager
from time import time

from fastapi.testclient import TestClient

from app.main import app


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


class _StubSettings:
    stripe_webhook_secret = "whsec_story29_test"
    stripe_webhook_tolerance_seconds = 300


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


def test_stripe_webhook_accepts_valid_signature_and_routes_verified_event():
    payload = json.dumps(
        {
            "id": "evt_story29_valid",
            "type": "invoice.payment_succeeded",
            "account": "acct_story29_valid",
            "data": {"object": {"id": "in_story29_valid"}},
        }
    ).encode("utf-8")
    signature_header = _stripe_signature_header(
        payload=payload,
        secret=_StubSettings.stripe_webhook_secret,
    )
    capture_router = _CaptureStripeWebhookRouter()

    with TestClient(app) as client:
        with _override_app_state("settings", _StubSettings()):
            with _override_app_state("stripe_webhook_router", capture_router):
                response = client.post(
                    "/webhooks/stripe",
                    content=payload,
                    headers={
                        "Content-Type": "application/json",
                        "Stripe-Signature": signature_header,
                    },
                )

    assert response.status_code == 200
    assert response.headers.get("X-Request-Id")
    assert response.json() == {"status": "ok"}
    assert capture_router.events == [
        {
            "stripe_event_id": "evt_story29_valid",
            "stripe_account_id": "acct_story29_valid",
            "event_type": "invoice.payment_succeeded",
        }
    ]


def test_stripe_webhook_rejects_invalid_signature_without_routing():
    payload = json.dumps(
        {
            "id": "evt_story29_invalid",
            "type": "invoice.payment_succeeded",
            "account": "acct_story29_invalid",
            "data": {"object": {"id": "in_story29_invalid"}},
        }
    ).encode("utf-8")
    capture_router = _CaptureStripeWebhookRouter()

    with TestClient(app) as client:
        with _override_app_state("settings", _StubSettings()):
            with _override_app_state("stripe_webhook_router", capture_router):
                response = client.post(
                    "/webhooks/stripe",
                    content=payload,
                    headers={
                        "Content-Type": "application/json",
                        "Stripe-Signature": "t=123,v1=not-a-real-signature",
                    },
                )

    assert response.status_code == 400
    assert response.headers.get("X-Request-Id")
    assert response.json() == {"detail": "invalid stripe webhook signature"}
    assert capture_router.events == []


def test_stripe_webhook_accepts_verified_unsupported_event_type_as_safe_noop():
    payload = json.dumps(
        {
            "id": "evt_story29_noop",
            "type": "customer.subscription.paused",
            "account": "acct_story29_noop",
            "data": {"object": {"id": "sub_story29_noop"}},
        }
    ).encode("utf-8")
    signature_header = _stripe_signature_header(
        payload=payload,
        secret=_StubSettings.stripe_webhook_secret,
    )

    with TestClient(app) as client:
        with _override_app_state("settings", _StubSettings()):
            response = client.post(
                "/webhooks/stripe",
                content=payload,
                headers={
                    "Content-Type": "application/json",
                    "Stripe-Signature": signature_header,
                },
            )

    assert response.status_code == 200
    assert response.headers.get("X-Request-Id")
    assert response.json() == {"status": "ok"}
