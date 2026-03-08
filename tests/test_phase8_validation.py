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
from app.models.booking import Booking
from app.models.invoice import Invoice
from app.models.invoice_payment_event import InvoicePaymentEvent
from app.services.calendly_webhooks import build_default_calendly_webhook_router
from app.services.email_stub import get_magic_link_outbox
from app.services.invoice_payment_events import (
    InvoicePaymentEventService,
    UNATTRIBUTED_REASON_UNKNOWN_BOOKING_UUID,
)
from app.services.stripe_provider import StripeAccountReadiness, StripeInvoiceCreateResult


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


def _stripe_signature_header(*, payload: bytes, secret: str, timestamp: int | None = None) -> str:
    resolved_timestamp = timestamp or int(time())
    signed_payload = f"{resolved_timestamp}.".encode("utf-8") + payload
    signature = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
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


def _invoice_paid_payload(
    *,
    stripe_event_id: str,
    stripe_account_id: str,
    stripe_invoice_id: str,
    paid_at: datetime,
    metadata: dict[str, str] | None = None,
) -> bytes:
    return json.dumps(
        {
            "id": stripe_event_id,
            "type": "invoice.paid",
            "account": stripe_account_id,
            "data": {
                "object": {
                    "id": stripe_invoice_id,
                    "object": "invoice",
                    "status": "paid",
                    "status_transitions": {"paid_at": int(paid_at.timestamp())},
                    "metadata": metadata or {},
                }
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


class _SequencedStubStripeProvider:
    def __init__(
        self,
        *,
        account_id: str,
        readiness: StripeAccountReadiness,
        created_invoice_ids: list[str],
    ):
        self.account_id = account_id
        self.readiness = readiness
        self._created_invoice_ids = list(created_invoice_ids)
        self.onboarding_calls: list[dict[str, str]] = []
        self.exchange_calls: list[dict[str, str]] = []
        self.readiness_calls: list[str] = []
        self.create_calls: list[dict[str, object]] = []
        self.void_calls: list[dict[str, str]] = []

    def build_connect_onboarding_url(self, *, creator_id: str, state: str) -> str:
        self.onboarding_calls.append({"creator_id": creator_id, "state": state})
        return (
            "https://connect.stripe.com/oauth/authorize"
            f"?response_type=code&client_id=ca_test_story50&state={state}&creator_id={creator_id}"
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
        if not self._created_invoice_ids:
            raise AssertionError("No stubbed Stripe invoice id remaining for create_invoice")

        self.create_calls.append(
            {
                "stripe_account_id": stripe_account_id,
                "amount_cents": amount_cents,
                "currency": currency,
                "metadata": metadata,
                "idempotency_key": idempotency_key,
            }
        )
        return StripeInvoiceCreateResult(stripe_invoice_id=self._created_invoice_ids.pop(0))

    def void_invoice(self, *, stripe_account_id: str, stripe_invoice_id: str) -> None:
        self.void_calls.append(
            {
                "stripe_account_id": stripe_account_id,
                "stripe_invoice_id": stripe_invoice_id,
            }
        )


def test_phase8_payment_attribution_flow_end_to_end():
    engine = _engine()
    creator_email = f"phase8_creator_{uuid.uuid4().hex}@example.com"
    settings = get_settings()
    provider = _SequencedStubStripeProvider(
        account_id="acct_story50_connected",
        readiness=StripeAccountReadiness(charges_enabled=True),
        created_invoice_ids=["in_story50_paid", "in_story50_early"],
    )
    calendly_webhook_router = build_default_calendly_webhook_router(provider=provider)
    payment_event_service = InvoicePaymentEventService(session_factory=lambda: Session(engine))

    with _override_app_state("stripe_provider", provider):
        with _override_app_state("calendly_webhook_router", calendly_webhook_router):
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
                        "code": "auth_code_story50",
                        "state": connect_start_payload["state"],
                    },
                )
                me_response = client.get("/me", headers=auth_headers)
                creator_id = uuid.UUID(me_response.json()["id"])

                booking_link_response = client.post(
                    "/booking-links",
                    headers=auth_headers,
                    json={
                        "name": "Phase 8 Validation Call",
                        "calendly_url": "https://calendly.com/example/phase8-validation-call",
                        "billing_amount_cents": 19500,
                        "billing_currency": " usd ",
                    },
                )
                booking_link = booking_link_response.json()

                happy_content_response = client.post(
                    "/content",
                    headers=auth_headers,
                    json={
                        "source_url": "https://example.com/posts/phase8-paid-content",
                        "booking_link_id": booking_link["id"],
                    },
                )
                happy_content = happy_content_response.json()

                happy_created_payload = _invitee_created_payload(
                    event_id="EVT_story50_paid",
                    calendly_booking_uuid="BOOK_story50_paid",
                    tid=happy_content["tid"],
                    email="phase8-booked@example.com",
                    created_at="2026-03-08T22:00:00Z",
                )
                happy_created_signature = _calendly_signature_header(
                    payload=happy_created_payload,
                    signing_key=settings.calendly_webhook_signing_key,
                )
                happy_created_response = client.post(
                    "/webhooks/calendly",
                    content=happy_created_payload,
                    headers={
                        "Content-Type": "application/json",
                        "Calendly-Webhook-Signature": happy_created_signature,
                    },
                )

                with Session(engine) as db:
                    happy_booking = db.scalar(
                        select(Booking).where(Booking.calendly_booking_uuid == "BOOK_story50_paid")
                    )
                    happy_open_invoice = db.scalar(
                        select(Invoice).join(Booking).where(
                            Booking.calendly_booking_uuid == "BOOK_story50_paid"
                        )
                    )

                happy_paid_at = datetime(2026, 3, 8, 22, 30, tzinfo=timezone.utc)
                happy_paid_payload = _invoice_paid_payload(
                    stripe_event_id="evt_story50_paid",
                    stripe_account_id=provider.account_id,
                    stripe_invoice_id="in_story50_paid",
                    paid_at=happy_paid_at,
                )
                happy_paid_signature = _stripe_signature_header(
                    payload=happy_paid_payload,
                    secret=settings.stripe_webhook_secret,
                )
                happy_paid_response = client.post(
                    "/webhooks/stripe",
                    content=happy_paid_payload,
                    headers={
                        "Content-Type": "application/json",
                        "Stripe-Signature": happy_paid_signature,
                    },
                )
                duplicate_happy_paid_response = client.post(
                    "/webhooks/stripe",
                    content=happy_paid_payload,
                    headers={
                        "Content-Type": "application/json",
                        "Stripe-Signature": happy_paid_signature,
                    },
                )

                early_content_response = client.post(
                    "/content",
                    headers=auth_headers,
                    json={
                        "source_url": "https://example.com/posts/phase8-early-content",
                        "booking_link_id": booking_link["id"],
                    },
                )
                early_content = early_content_response.json()

                early_paid_at = datetime(2026, 3, 8, 22, 45, tzinfo=timezone.utc)
                early_paid_payload = _invoice_paid_payload(
                    stripe_event_id="evt_story50_early",
                    stripe_account_id=provider.account_id,
                    stripe_invoice_id="in_story50_early",
                    paid_at=early_paid_at,
                    metadata={
                        "booking_uuid": "BOOK_story50_early",
                        "tid": early_content["tid"],
                    },
                )
                early_paid_signature = _stripe_signature_header(
                    payload=early_paid_payload,
                    secret=settings.stripe_webhook_secret,
                )
                early_paid_response = client.post(
                    "/webhooks/stripe",
                    content=early_paid_payload,
                    headers={
                        "Content-Type": "application/json",
                        "Stripe-Signature": early_paid_signature,
                    },
                )

                early_summary_before_reconciliation = payment_event_service.summarize_paid_revenue(
                    creator_id=creator_id
                )

                with Session(engine) as db:
                    unmatched_payment_event = db.scalar(
                        select(InvoicePaymentEvent).where(
                            InvoicePaymentEvent.stripe_event_id == "evt_story50_early"
                        )
                    )

                early_created_payload = _invitee_created_payload(
                    event_id="EVT_story50_early",
                    calendly_booking_uuid="BOOK_story50_early",
                    tid=early_content["tid"],
                    email="phase8-early@example.com",
                    created_at="2026-03-08T22:50:00Z",
                )
                early_created_signature = _calendly_signature_header(
                    payload=early_created_payload,
                    signing_key=settings.calendly_webhook_signing_key,
                )
                early_created_response = client.post(
                    "/webhooks/calendly",
                    content=early_created_payload,
                    headers={
                        "Content-Type": "application/json",
                        "Calendly-Webhook-Signature": early_created_signature,
                    },
                )

    with Session(engine) as db:
        happy_paid_invoice = db.scalar(
            select(Invoice).join(Booking).where(Booking.calendly_booking_uuid == "BOOK_story50_paid")
        )
        happy_payment_events = db.scalars(
            select(InvoicePaymentEvent).where(
                InvoicePaymentEvent.stripe_event_id == "evt_story50_paid"
            )
        ).all()
        happy_payment_event_booking_uuid = db.scalar(
            select(Booking.calendly_booking_uuid).where(
                Booking.id == happy_payment_events[0].booking_id
            )
        )
        early_open_invoice = db.scalar(
            select(Invoice).join(Booking).where(Booking.calendly_booking_uuid == "BOOK_story50_early")
        )

    reconciliation = payment_event_service.reconcile_unmatched_payment_event(
        stripe_event_id="evt_story50_early"
    )
    duplicate_reconciliation = payment_event_service.reconcile_unmatched_payment_event(
        stripe_event_id="evt_story50_early"
    )
    final_summary = payment_event_service.summarize_paid_revenue(creator_id=creator_id)

    with Session(engine) as db:
        reconciled_invoice = db.scalar(
            select(Invoice).join(Booking).where(Booking.calendly_booking_uuid == "BOOK_story50_early")
        )
        reconciled_payment_event = db.scalar(
            select(InvoicePaymentEvent).where(
                InvoicePaymentEvent.stripe_event_id == "evt_story50_early"
            )
        )
        reconciled_payment_event_booking_uuid = db.scalar(
            select(Booking.calendly_booking_uuid).where(
                Booking.id == reconciled_payment_event.booking_id
            )
        )
        all_payment_events = db.scalars(
            select(InvoicePaymentEvent).order_by(InvoicePaymentEvent.stripe_event_id.asc())
        ).all()

    assert start_response.status_code == 200
    assert start_response.json() == {"status": "ok"}

    assert verify_response.status_code == 200
    assert verify_response.json()["token_type"] == "bearer"

    assert connect_start_response.status_code == 200
    assert connect_start_payload["state"]
    assert connect_start_payload["onboarding_url"].startswith(
        "https://connect.stripe.com/oauth/authorize"
    )
    assert callback_response.status_code == 200
    assert callback_response.json() == {"status": "ok"}

    assert me_response.status_code == 200
    assert me_response.json()["email"] == creator_email
    assert me_response.json()["stripe_connect_status"] == "connected"
    assert me_response.json()["stripe_account_id"] == provider.account_id
    assert me_response.json()["stripe_connected_at"] is not None

    assert booking_link_response.status_code == 201
    assert booking_link["billing_amount_cents"] == 19500
    assert booking_link["billing_currency"] == "USD"

    assert happy_content_response.status_code == 201
    assert early_content_response.status_code == 201

    assert happy_created_response.status_code == 200
    assert happy_created_response.json() == {"status": "ok"}
    assert happy_booking is not None
    assert happy_booking.tid == happy_content["tid"]
    assert happy_booking.status == "created"
    assert happy_open_invoice is not None
    assert happy_open_invoice.status == "open"
    assert happy_open_invoice.tid == happy_content["tid"]
    assert happy_open_invoice.stripe_invoice_id == "in_story50_paid"

    assert happy_paid_response.status_code == 200
    assert happy_paid_response.json() == {"status": "ok"}
    assert duplicate_happy_paid_response.status_code == 200
    assert duplicate_happy_paid_response.json() == {"status": "ok"}
    assert happy_paid_invoice is not None
    assert happy_paid_invoice.status == "paid"
    assert happy_paid_invoice.paid_at == happy_paid_at
    assert len(happy_payment_events) == 1
    assert happy_payment_events[0].invoice_id == happy_paid_invoice.id
    assert happy_payment_events[0].creator_id == creator_id
    assert happy_payment_events[0].booking_id == happy_paid_invoice.booking_id
    assert happy_payment_event_booking_uuid == "BOOK_story50_paid"
    assert happy_payment_events[0].tid == happy_content["tid"]
    assert happy_payment_events[0].status == "applied"
    assert happy_payment_events[0].paid_at == happy_paid_at

    assert early_paid_response.status_code == 200
    assert early_paid_response.json() == {"status": "ok"}
    assert unmatched_payment_event is not None
    assert unmatched_payment_event.status == "unmatched"
    assert unmatched_payment_event.creator_id == creator_id
    assert unmatched_payment_event.booking_id is None
    assert unmatched_payment_event.tid is None
    assert unmatched_payment_event.unattributed_reason == UNATTRIBUTED_REASON_UNKNOWN_BOOKING_UUID
    assert early_summary_before_reconciliation.attributed_total_cents == 19500
    assert [
        (item.tid, item.amount_cents)
        for item in early_summary_before_reconciliation.attributed_revenue_by_tid
    ] == [(happy_content["tid"], 19500)]
    assert [
        (item.reason, item.amount_cents, item.event_count)
        for item in early_summary_before_reconciliation.unattributed_revenue_by_reason
    ] == [(UNATTRIBUTED_REASON_UNKNOWN_BOOKING_UUID, 0, 1)]
    assert early_summary_before_reconciliation.unattributed_total_cents == 0
    assert early_summary_before_reconciliation.unattributed_event_count == 1

    assert early_created_response.status_code == 200
    assert early_created_response.json() == {"status": "ok"}
    assert early_open_invoice is not None
    assert early_open_invoice.status == "open"
    assert early_open_invoice.stripe_invoice_id == "in_story50_early"
    assert early_open_invoice.tid == early_content["tid"]
    assert early_open_invoice.paid_at is None

    assert reconciliation.outcome == "reconciled"
    assert reconciliation.invoice_id == early_open_invoice.id
    assert reconciliation.creator_id == creator_id
    assert reconciliation.booking_uuid == "BOOK_story50_early"
    assert reconciliation.tid == early_content["tid"]
    assert duplicate_reconciliation.outcome == "already_reconciled"

    assert reconciled_invoice is not None
    assert reconciled_invoice.id == early_open_invoice.id
    assert reconciled_invoice.status == "paid"
    assert reconciled_invoice.paid_at == early_paid_at

    assert reconciled_payment_event is not None
    assert reconciled_payment_event.invoice_id == early_open_invoice.id
    assert reconciled_payment_event.creator_id == creator_id
    assert reconciled_payment_event.booking_id == early_open_invoice.booking_id
    assert reconciled_payment_event_booking_uuid == "BOOK_story50_early"
    assert reconciled_payment_event.tid == early_content["tid"]
    assert reconciled_payment_event.status == "reconciled"
    assert reconciled_payment_event.unattributed_reason is None
    assert reconciled_payment_event.paid_at == early_paid_at

    assert [(event.stripe_event_id, event.status) for event in all_payment_events] == [
        ("evt_story50_early", "reconciled"),
        ("evt_story50_paid", "applied"),
    ]
    assert sorted(
        (item.tid, item.amount_cents) for item in final_summary.attributed_revenue_by_tid
    ) == sorted(
        [
            (early_content["tid"], 19500),
            (happy_content["tid"], 19500),
        ]
    )
    assert final_summary.attributed_total_cents == 39000
    assert final_summary.unattributed_revenue_by_reason == []
    assert final_summary.unattributed_total_cents == 0
    assert final_summary.unattributed_event_count == 0

    assert provider.onboarding_calls == [
        {
            "creator_id": me_response.json()["id"],
            "state": connect_start_payload["state"],
        }
    ]
    assert provider.exchange_calls == [
        {
            "code": "auth_code_story50",
            "state": connect_start_payload["state"],
        }
    ]
    assert provider.readiness_calls == [provider.account_id, provider.account_id]
    assert provider.create_calls == [
        {
            "stripe_account_id": provider.account_id,
            "amount_cents": 19500,
            "currency": "USD",
            "metadata": {
                "creator_id": me_response.json()["id"],
                "booking_uuid": "BOOK_story50_paid",
                "tid": happy_content["tid"],
            },
            "idempotency_key": "billing:create:BOOK_story50_paid",
        },
        {
            "stripe_account_id": provider.account_id,
            "amount_cents": 19500,
            "currency": "USD",
            "metadata": {
                "creator_id": me_response.json()["id"],
                "booking_uuid": "BOOK_story50_early",
                "tid": early_content["tid"],
            },
            "idempotency_key": "billing:create:BOOK_story50_early",
        },
    ]
    assert provider.void_calls == []
