import hashlib
import hmac
import json
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from time import time
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.main import app
from app.models.booking import Booking
from app.models.invoice import Invoice
from app.services.calendly_webhooks import build_default_calendly_webhook_router
from app.services.email_stub import get_magic_link_outbox
from app.services.stripe_connect import decode_stripe_connect_state
from app.services.stripe_provider import (
    StripeAccountReadiness,
    StripeInvoiceCreateResult,
)


def _engine():
    return create_engine(os.environ["TEST_DATABASE_URL"])


def _latest_magic_link_token_for_email(email: str) -> str:
    outbox = get_magic_link_outbox()
    for message in reversed(outbox):
        if message["email"] == email:
            return message["token"]

    raise AssertionError(f"No magic-link token captured for {email}")


def _calendly_signature_header(*, payload: bytes, signing_key: str, timestamp: int | None = None) -> str:
    resolved_timestamp = timestamp or int(time())
    signed_payload = f"{resolved_timestamp}.".encode("utf-8") + payload
    signature = hmac.new(signing_key.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return f"t={resolved_timestamp},v1={signature}"


def _invitee_created_payload(
    *,
    event_id: str,
    calendly_booking_uuid: str,
    tid: str,
    email: str,
    created_at: str,
) -> bytes:
    return json.dumps(
        {
            "event": "invitee.created",
            "payload": {
                "event": f"https://api.calendly.com/scheduled_events/{event_id}",
                "uri": (
                    "https://api.calendly.com/scheduled_events/"
                    f"{event_id}/invitees/{calendly_booking_uuid}"
                ),
                "email": email,
                "created_at": created_at,
                "tracking": {"utm_content": tid},
            },
        }
    ).encode("utf-8")


def _invitee_canceled_payload(
    *,
    event_id: str,
    calendly_booking_uuid: str,
    tid: str,
    canceled_at: str,
) -> bytes:
    return json.dumps(
        {
            "event": "invitee.canceled",
            "payload": {
                "event": f"https://api.calendly.com/scheduled_events/{event_id}",
                "uri": (
                    "https://api.calendly.com/scheduled_events/"
                    f"{event_id}/invitees/{calendly_booking_uuid}"
                ),
                "tracking": {"utm_content": tid},
                "canceled_at": canceled_at,
            },
        }
    ).encode("utf-8")


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
    def __init__(
        self,
        *,
        account_id: str,
        readiness: StripeAccountReadiness,
        created_invoice_id: str,
    ):
        self.account_id = account_id
        self.readiness = readiness
        self.created_invoice_id = created_invoice_id
        self.onboarding_calls: list[dict[str, str]] = []
        self.exchange_calls: list[dict[str, str]] = []
        self.readiness_calls: list[str] = []
        self.create_calls: list[dict[str, object]] = []
        self.void_calls: list[dict[str, str]] = []

    def build_connect_onboarding_url(self, *, creator_id: str, state: str) -> str:
        self.onboarding_calls.append({"creator_id": creator_id, "state": state})
        return (
            "https://connect.stripe.com/oauth/authorize"
            f"?response_type=code&client_id=ca_test_story46&state={state}&creator_id={creator_id}"
        )

    def exchange_connect_callback(self, *, code: str, state: str) -> str:
        self.exchange_calls.append({"code": code, "state": state})
        return self.account_id

    def get_account_readiness(self, *, stripe_account_id: str) -> StripeAccountReadiness:
        self.readiness_calls.append(stripe_account_id)
        return self.readiness

    def create_invoice(
        self,
        *,
        stripe_account_id: str,
        amount_cents: int,
        currency: str,
        metadata: dict[str, str],
        idempotency_key: str,
    ) -> StripeInvoiceCreateResult:
        self.create_calls.append(
            {
                "stripe_account_id": stripe_account_id,
                "amount_cents": amount_cents,
                "currency": currency,
                "metadata": metadata,
                "idempotency_key": idempotency_key,
            }
        )
        return StripeInvoiceCreateResult(stripe_invoice_id=self.created_invoice_id)

    def void_invoice(self, *, stripe_account_id: str, stripe_invoice_id: str) -> None:
        self.void_calls.append(
            {
                "stripe_account_id": stripe_account_id,
                "stripe_invoice_id": stripe_invoice_id,
            }
        )


def test_phase7_invoice_creation_flow_end_to_end():
    creator_email = f"phase7_creator_{uuid.uuid4().hex}@example.com"
    settings = get_settings()
    provider = _StubStripeProvider(
        account_id="acct_story46_connected",
        readiness=StripeAccountReadiness(charges_enabled=True),
        created_invoice_id="in_story46_open",
    )
    webhook_router = build_default_calendly_webhook_router(provider=provider)

    with _override_app_state("stripe_provider", provider):
        with _override_app_state("calendly_webhook_router", webhook_router):
            with TestClient(app) as client:
                start_response = client.post(
                    "/auth/magic-link/start",
                    json={"email": creator_email},
                )
                verify_response = client.get(
                    "/auth/magic-link/verify",
                    params={"token": _latest_magic_link_token_for_email(creator_email)},
                )
                access_token = verify_response.json()["access_token"]
                auth_headers = {"Authorization": f"Bearer {access_token}"}

                connect_start_response = client.post(
                    "/stripe/connect/start",
                    headers=auth_headers,
                )
                connect_start_payload = connect_start_response.json()
                callback_response = client.get(
                    "/stripe/connect/callback",
                    params={
                        "code": "auth_code_story46",
                        "state": connect_start_payload["state"],
                    },
                )
                me_response = client.get("/me", headers=auth_headers)

                billed_booking_link_response = client.post(
                    "/booking-links",
                    headers=auth_headers,
                    json={
                        "name": "Phase 7 Validation Call",
                        "calendly_url": "https://calendly.com/example/phase7-validation-call",
                        "billing_amount_cents": 19500,
                        "billing_currency": " usd ",
                    },
                )
                billed_booking_link = billed_booking_link_response.json()
                billed_content_response = client.post(
                    "/content",
                    headers=auth_headers,
                    json={
                        "source_url": "https://example.com/posts/phase7-validation-content",
                        "booking_link_id": billed_booking_link["id"],
                    },
                )
                billed_content = billed_content_response.json()

                created_payload = _invitee_created_payload(
                    event_id="EVT_phase7_valid",
                    calendly_booking_uuid="BOOK_phase7_valid",
                    tid=billed_content["tid"],
                    email="phase7-booked@example.com",
                    created_at="2026-03-08T21:00:00Z",
                )
                created_signature = _calendly_signature_header(
                    payload=created_payload,
                    signing_key=settings.calendly_webhook_signing_key,
                )
                first_created_response = client.post(
                    "/webhooks/calendly",
                    content=created_payload,
                    headers={
                        "Content-Type": "application/json",
                        "Calendly-Webhook-Signature": created_signature,
                    },
                )
                duplicate_created_response = client.post(
                    "/webhooks/calendly",
                    content=created_payload,
                    headers={
                        "Content-Type": "application/json",
                        "Calendly-Webhook-Signature": created_signature,
                    },
                )

                with Session(_engine()) as db:
                    open_booking = db.scalar(
                        select(Booking).where(Booking.calendly_booking_uuid == "BOOK_phase7_valid")
                    )
                    open_invoice = db.scalar(
                        select(Invoice).join(Booking).where(
                            Booking.calendly_booking_uuid == "BOOK_phase7_valid"
                        )
                    )

                canceled_payload = _invitee_canceled_payload(
                    event_id="EVT_phase7_valid",
                    calendly_booking_uuid="BOOK_phase7_valid",
                    tid=billed_content["tid"],
                    canceled_at="2026-03-08T21:30:00Z",
                )
                canceled_signature = _calendly_signature_header(
                    payload=canceled_payload,
                    signing_key=settings.calendly_webhook_signing_key,
                )
                canceled_response = client.post(
                    "/webhooks/calendly",
                    content=canceled_payload,
                    headers={
                        "Content-Type": "application/json",
                        "Calendly-Webhook-Signature": canceled_signature,
                    },
                )
                duplicate_canceled_response = client.post(
                    "/webhooks/calendly",
                    content=canceled_payload,
                    headers={
                        "Content-Type": "application/json",
                        "Calendly-Webhook-Signature": canceled_signature,
                    },
                )

                blocked_booking_link_response = client.post(
                    "/booking-links",
                    headers=auth_headers,
                    json={
                        "name": "Phase 7 Blocked Call",
                        "calendly_url": "https://calendly.com/example/phase7-blocked-call",
                    },
                )
                blocked_booking_link = blocked_booking_link_response.json()
                blocked_content_response = client.post(
                    "/content",
                    headers=auth_headers,
                    json={
                        "source_url": "https://example.com/posts/phase7-blocked-content",
                        "booking_link_id": blocked_booking_link["id"],
                    },
                )
                blocked_content = blocked_content_response.json()

                blocked_payload = _invitee_created_payload(
                    event_id="EVT_phase7_blocked",
                    calendly_booking_uuid="BOOK_phase7_blocked",
                    tid=blocked_content["tid"],
                    email="phase7-blocked@example.com",
                    created_at="2026-03-08T21:45:00Z",
                )
                blocked_signature = _calendly_signature_header(
                    payload=blocked_payload,
                    signing_key=settings.calendly_webhook_signing_key,
                )
                with patch("app.services.billing.logger.info") as billing_info_log:
                    blocked_response = client.post(
                        "/webhooks/calendly",
                        content=blocked_payload,
                        headers={
                            "Content-Type": "application/json",
                            "Calendly-Webhook-Signature": blocked_signature,
                        },
                    )

    decoded_state = decode_stripe_connect_state(connect_start_payload["state"])

    with Session(_engine()) as db:
        persisted_bookings = db.scalars(
            select(Booking).order_by(Booking.calendly_booking_uuid.asc())
        ).all()
        happy_booking = db.scalar(
            select(Booking).where(Booking.calendly_booking_uuid == "BOOK_phase7_valid")
        )
        blocked_booking = db.scalar(
            select(Booking).where(Booking.calendly_booking_uuid == "BOOK_phase7_blocked")
        )
        persisted_invoices = db.scalars(select(Invoice).order_by(Invoice.booking_id.asc())).all()
        happy_invoice = db.scalar(
            select(Invoice).join(Booking).where(Booking.calendly_booking_uuid == "BOOK_phase7_valid")
        )

    assert start_response.status_code == 200
    assert start_response.headers.get("X-Request-Id")
    assert start_response.json() == {"status": "ok"}

    assert verify_response.status_code == 200
    assert verify_response.headers.get("X-Request-Id")
    assert verify_response.json()["token_type"] == "bearer"

    assert connect_start_response.status_code == 200
    assert connect_start_response.headers.get("X-Request-Id")
    assert connect_start_payload["state"]
    assert connect_start_payload["onboarding_url"].startswith(
        "https://connect.stripe.com/oauth/authorize"
    )
    assert f"state={connect_start_payload['state']}" in connect_start_payload["onboarding_url"]
    assert decoded_state["sub"] == me_response.json()["id"]
    assert decoded_state["purpose"] == "stripe_connect"
    assert provider.onboarding_calls == [
        {
            "creator_id": me_response.json()["id"],
            "state": connect_start_payload["state"],
        }
    ]

    assert callback_response.status_code == 200
    assert callback_response.headers.get("X-Request-Id")
    assert callback_response.json() == {"status": "ok"}
    assert provider.exchange_calls == [
        {
            "code": "auth_code_story46",
            "state": connect_start_payload["state"],
        }
    ]

    assert me_response.status_code == 200
    assert me_response.headers.get("X-Request-Id")
    assert me_response.json()["email"] == creator_email
    assert me_response.json()["stripe_connect_status"] == "connected"
    assert me_response.json()["stripe_account_id"] == provider.account_id
    assert me_response.json()["stripe_connected_at"] is not None

    assert billed_booking_link_response.status_code == 201
    assert billed_booking_link_response.headers.get("X-Request-Id")
    assert billed_booking_link["name"] == "Phase 7 Validation Call"
    assert billed_booking_link["billing_amount_cents"] == 19500
    assert billed_booking_link["billing_currency"] == "USD"

    assert billed_content_response.status_code == 201
    assert billed_content_response.headers.get("X-Request-Id")
    assert billed_content["booking_link_id"] == billed_booking_link["id"]

    assert first_created_response.status_code == 200
    assert first_created_response.headers.get("X-Request-Id")
    assert first_created_response.json() == {"status": "ok"}

    assert duplicate_created_response.status_code == 200
    assert duplicate_created_response.headers.get("X-Request-Id")
    assert duplicate_created_response.json() == {"status": "ok"}

    assert open_booking is not None
    assert str(open_booking.creator_id) == me_response.json()["id"]
    assert str(open_booking.booking_link_id) == billed_booking_link["id"]
    assert open_booking.tid == billed_content["tid"]
    assert open_booking.email == "phase7-booked@example.com"
    assert open_booking.status == "created"
    assert open_booking.frozen_billing_amount_cents == 19500
    assert open_booking.frozen_billing_currency == "USD"
    assert open_booking.booked_at == datetime(2026, 3, 8, 21, 0, tzinfo=timezone.utc)
    assert open_booking.canceled_at is None

    assert open_invoice is not None
    assert open_invoice.creator_id == open_booking.creator_id
    assert open_invoice.booking_id == open_booking.id
    assert open_invoice.tid == billed_content["tid"]
    assert open_invoice.stripe_account_id == provider.account_id
    assert open_invoice.stripe_invoice_id == "in_story46_open"
    assert open_invoice.amount_cents == 19500
    assert open_invoice.currency == "USD"
    assert open_invoice.status == "open"
    assert open_invoice.voided_at is None

    assert canceled_response.status_code == 200
    assert canceled_response.headers.get("X-Request-Id")
    assert canceled_response.json() == {"status": "ok"}

    assert duplicate_canceled_response.status_code == 200
    assert duplicate_canceled_response.headers.get("X-Request-Id")
    assert duplicate_canceled_response.json() == {"status": "ok"}

    assert blocked_booking_link_response.status_code == 201
    assert blocked_booking_link_response.headers.get("X-Request-Id")
    assert blocked_booking_link["billing_amount_cents"] is None
    assert blocked_booking_link["billing_currency"] is None

    assert blocked_content_response.status_code == 201
    assert blocked_content_response.headers.get("X-Request-Id")
    assert blocked_content["booking_link_id"] == blocked_booking_link["id"]

    assert blocked_response.status_code == 200
    assert blocked_response.headers.get("X-Request-Id")
    assert blocked_response.json() == {"status": "ok"}

    assert len(persisted_bookings) == 2
    assert happy_booking is not None
    assert blocked_booking is not None
    assert happy_booking.status == "canceled"
    assert happy_booking.frozen_billing_amount_cents == 19500
    assert happy_booking.frozen_billing_currency == "USD"
    assert happy_booking.canceled_at == datetime(2026, 3, 8, 21, 30, tzinfo=timezone.utc)
    assert blocked_booking.status == "created"
    assert blocked_booking.frozen_billing_amount_cents is None
    assert blocked_booking.frozen_billing_currency is None
    assert blocked_booking.canceled_at is None
    assert str(blocked_booking.creator_id) == me_response.json()["id"]
    assert str(blocked_booking.booking_link_id) == blocked_booking_link["id"]
    assert blocked_booking.tid == blocked_content["tid"]
    assert blocked_booking.email == "phase7-blocked@example.com"

    assert len(persisted_invoices) == 1
    assert happy_invoice is not None
    assert happy_invoice.booking_id == happy_booking.id
    assert happy_invoice.status == "void"
    assert happy_invoice.voided_at is not None

    assert provider.readiness_calls == [provider.account_id]
    assert provider.create_calls == [
        {
            "stripe_account_id": provider.account_id,
            "amount_cents": 19500,
            "currency": "USD",
            "metadata": {
                "creator_id": me_response.json()["id"],
                "booking_provider": "calendly",
                "provider_booking_id": "BOOK_phase7_valid",
                "booking_uuid": "BOOK_phase7_valid",
                "tid": billed_content["tid"],
            },
            "idempotency_key": "billing:create:calendly:BOOK_phase7_valid",
        }
    ]
    assert provider.void_calls == [
        {
            "stripe_account_id": provider.account_id,
            "stripe_invoice_id": "in_story46_open",
        }
    ]

    billing_info_log.assert_called_once()
    assert (
        billing_info_log.call_args.args[0]
        == "billing_invoice_create_deferred_missing_billing_defaults booking_id=%s creator_id=%s booking_link_id=%s missing_amount=%s missing_currency=%s"
    )
    assert str(billing_info_log.call_args.args[1]) == str(blocked_booking.id)
    assert str(billing_info_log.call_args.args[2]) == me_response.json()["id"]
    assert str(billing_info_log.call_args.args[3]) == blocked_booking_link["id"]
    assert billing_info_log.call_args.args[4] is True
    assert billing_info_log.call_args.args[5] is True
