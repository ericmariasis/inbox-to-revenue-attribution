import hashlib
import hmac
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from time import time

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.main import app
from app.models.booking import Booking
from app.models.booking_link import BookingLink
from app.models.content import Content
from app.models.creator import Creator
from app.models.invoice import Invoice
from app.models.invoice_payment_event import InvoicePaymentEvent
from app.services.invoice_payment_events import (
    UNATTRIBUTED_REASON_MISSING_TID,
    UNATTRIBUTED_REASON_UNKNOWN_BOOKING_UUID,
    UNATTRIBUTED_REASON_UNKNOWN_STRIPE_INVOICE_ID,
)


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


def _engine():
    return create_engine(os.environ["TEST_DATABASE_URL"])


def _persist_open_invoice(
    *,
    stripe_account_id: str,
    stripe_invoice_id: str,
    booking_uuid: str,
    tid: str,
) -> Invoice:
    with Session(_engine()) as session:
        creator = Creator(
            name="Story 48 Stripe Webhook Creator",
            stripe_connect_status="connected",
            stripe_account_id=stripe_account_id,
        )
        session.add(creator)
        session.flush()

        booking_link = BookingLink(
            creator_id=creator.id,
            name="Story 48 Stripe Webhook Link",
            calendly_url="https://calendly.com/example/story48-stripe-webhook",
        )
        session.add(booking_link)
        session.flush()

        content = Content(
            creator_id=creator.id,
            booking_link_id=booking_link.id,
            source_url="https://example.com/story48-stripe-webhook",
            tid=tid,
        )
        session.add(content)
        session.flush()

        booking = Booking(
            creator_id=creator.id,
            tid=content.tid,
            booking_link_id=booking_link.id,
            calendly_booking_uuid=booking_uuid,
            email="story48-booked@example.com",
            status="created",
            booked_at=datetime(2026, 3, 8, 22, 0, tzinfo=timezone.utc),
        )
        session.add(booking)
        session.flush()

        invoice = Invoice(
            creator_id=creator.id,
            booking_id=booking.id,
            tid=content.tid,
            stripe_account_id=stripe_account_id,
            stripe_invoice_id=stripe_invoice_id,
            amount_cents=19500,
            currency="USD",
            status="open",
            issued_at=datetime(2026, 3, 8, 22, 5, tzinfo=timezone.utc),
        )
        session.add(invoice)
        session.commit()
        session.refresh(invoice)
        return invoice


def _persist_creator(*, stripe_account_id: str) -> Creator:
    with Session(_engine()) as session:
        creator = Creator(
            name="Story 49 Stripe Webhook Creator",
            stripe_connect_status="connected",
            stripe_account_id=stripe_account_id,
        )
        session.add(creator)
        session.commit()
        session.refresh(creator)
        return creator


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


def test_stripe_webhook_invoice_paid_marks_matched_invoice_paid_and_persists_linked_payment_event():
    invoice = _persist_open_invoice(
        stripe_account_id="acct_story48_matched",
        stripe_invoice_id="in_story48_matched",
        booking_uuid="BOOK_story48_matched",
        tid="story48_tid_matched",
    )
    paid_at = datetime(2026, 3, 8, 22, 30, tzinfo=timezone.utc)
    payload = _invoice_paid_payload(
        stripe_event_id="evt_story48_matched",
        stripe_account_id="acct_story48_matched",
        stripe_invoice_id="in_story48_matched",
        paid_at=paid_at,
    )
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

    with Session(_engine()) as session:
        persisted_invoice = session.get(Invoice, invoice.id)
        payment_events = session.scalars(
            select(InvoicePaymentEvent).where(
                InvoicePaymentEvent.stripe_invoice_id == "in_story48_matched"
            )
        ).all()

    assert response.status_code == 200
    assert response.headers.get("X-Request-Id")
    assert response.json() == {"status": "ok"}
    assert persisted_invoice is not None
    assert persisted_invoice.status == "paid"
    assert persisted_invoice.paid_at == paid_at
    assert len(payment_events) == 1
    assert payment_events[0].payment_provider == "stripe"
    assert payment_events[0].provider_event_id == "evt_story48_matched"
    assert payment_events[0].provider_event_type == "invoice.paid"
    assert payment_events[0].provider_account_id == "acct_story48_matched"
    assert payment_events[0].provider_invoice_id == "in_story48_matched"
    assert payment_events[0].stripe_event_id == "evt_story48_matched"
    assert payment_events[0].stripe_event_type == "invoice.paid"
    assert payment_events[0].stripe_account_id == "acct_story48_matched"
    assert payment_events[0].stripe_invoice_id == "in_story48_matched"
    assert payment_events[0].invoice_id == invoice.id
    assert payment_events[0].creator_id == invoice.creator_id
    assert payment_events[0].booking_id == invoice.booking_id
    assert payment_events[0].tid == "story48_tid_matched"
    assert payment_events[0].status == "applied"
    assert payment_events[0].unattributed_reason is None
    assert payment_events[0].paid_at == paid_at
    assert payment_events[0].received_at is not None
    assert payment_events[0].processed_at is not None


def test_stripe_webhook_duplicate_stripe_event_id_delivery_is_idempotent_for_invoice_paid():
    invoice = _persist_open_invoice(
        stripe_account_id="acct_story48_duplicate",
        stripe_invoice_id="in_story48_duplicate",
        booking_uuid="BOOK_story48_duplicate",
        tid="story48_tid_duplicate",
    )
    paid_at = datetime(2026, 3, 8, 22, 40, tzinfo=timezone.utc)
    payload = _invoice_paid_payload(
        stripe_event_id="evt_story48_duplicate",
        stripe_account_id="acct_story48_duplicate",
        stripe_invoice_id="in_story48_duplicate",
        paid_at=paid_at,
    )
    signature_header = _stripe_signature_header(
        payload=payload,
        secret=_StubSettings.stripe_webhook_secret,
    )

    with TestClient(app) as client:
        with _override_app_state("settings", _StubSettings()):
            first_response = client.post(
                "/webhooks/stripe",
                content=payload,
                headers={
                    "Content-Type": "application/json",
                    "Stripe-Signature": signature_header,
                },
            )
            second_response = client.post(
                "/webhooks/stripe",
                content=payload,
                headers={
                    "Content-Type": "application/json",
                    "Stripe-Signature": signature_header,
                },
            )

    with Session(_engine()) as session:
        persisted_invoice = session.get(Invoice, invoice.id)
        payment_events = session.scalars(
            select(InvoicePaymentEvent).where(
                InvoicePaymentEvent.stripe_invoice_id == "in_story48_duplicate"
            )
        ).all()

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert persisted_invoice is not None
    assert persisted_invoice.status == "paid"
    assert persisted_invoice.paid_at == paid_at
    assert len(payment_events) == 1
    assert payment_events[0].stripe_event_id == "evt_story48_duplicate"


def test_stripe_webhook_invoice_paid_persists_one_unmatched_payment_event_when_local_invoice_is_missing():
    creator = _persist_creator(stripe_account_id="acct_story49_unmatched")
    paid_at = datetime(2026, 3, 8, 22, 45, tzinfo=timezone.utc)
    payload = _invoice_paid_payload(
        stripe_event_id="evt_story49_unmatched",
        stripe_account_id="acct_story49_unmatched",
        stripe_invoice_id="in_story49_unmatched",
        paid_at=paid_at,
        metadata={"tid": "story49_unmatched_tid"},
    )
    signature_header = _stripe_signature_header(
        payload=payload,
        secret=_StubSettings.stripe_webhook_secret,
    )

    with TestClient(app) as client:
        with _override_app_state("settings", _StubSettings()):
            first_response = client.post(
                "/webhooks/stripe",
                content=payload,
                headers={
                    "Content-Type": "application/json",
                    "Stripe-Signature": signature_header,
                },
            )
            second_response = client.post(
                "/webhooks/stripe",
                content=payload,
                headers={
                    "Content-Type": "application/json",
                    "Stripe-Signature": signature_header,
                },
            )

    with Session(_engine()) as session:
        payment_events = session.scalars(
            select(InvoicePaymentEvent).where(
                InvoicePaymentEvent.stripe_event_id == "evt_story49_unmatched"
            )
        ).all()
        invoice_count = session.query(Invoice).count()

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert invoice_count == 0
    assert len(payment_events) == 1
    assert payment_events[0].payment_provider == "stripe"
    assert payment_events[0].provider_event_id == "evt_story49_unmatched"
    assert payment_events[0].provider_event_type == "invoice.paid"
    assert payment_events[0].provider_account_id == "acct_story49_unmatched"
    assert payment_events[0].provider_invoice_id == "in_story49_unmatched"
    assert payment_events[0].status == "unmatched"
    assert payment_events[0].unattributed_reason == UNATTRIBUTED_REASON_UNKNOWN_STRIPE_INVOICE_ID
    assert payment_events[0].invoice_id is None
    assert payment_events[0].creator_id == creator.id
    assert payment_events[0].booking_id is None
    assert payment_events[0].tid is None
    assert payment_events[0].paid_at == paid_at
    assert payment_events[0].processed_at is None


def test_stripe_webhook_invoice_paid_records_missing_tid_reason_for_unmatched_event():
    creator = _persist_creator(stripe_account_id="acct_story49_missing_tid")
    paid_at = datetime(2026, 3, 8, 22, 46, tzinfo=timezone.utc)
    payload = _invoice_paid_payload(
        stripe_event_id="evt_story49_missing_tid",
        stripe_account_id="acct_story49_missing_tid",
        stripe_invoice_id="in_story49_missing_tid",
        paid_at=paid_at,
    )
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

    with Session(_engine()) as session:
        payment_event = session.scalar(
            select(InvoicePaymentEvent).where(
                InvoicePaymentEvent.stripe_event_id == "evt_story49_missing_tid"
            )
        )

    assert response.status_code == 200
    assert payment_event is not None
    assert payment_event.payment_provider == "stripe"
    assert payment_event.provider_event_id == "evt_story49_missing_tid"
    assert payment_event.provider_event_type == "invoice.paid"
    assert payment_event.provider_account_id == "acct_story49_missing_tid"
    assert payment_event.provider_invoice_id == "in_story49_missing_tid"
    assert payment_event.status == "unmatched"
    assert payment_event.unattributed_reason == UNATTRIBUTED_REASON_MISSING_TID
    assert payment_event.creator_id == creator.id


def test_stripe_webhook_invoice_paid_records_unknown_booking_uuid_reason_for_unmatched_event():
    creator = _persist_creator(stripe_account_id="acct_story49_unknown_booking")
    paid_at = datetime(2026, 3, 8, 22, 47, tzinfo=timezone.utc)
    payload = _invoice_paid_payload(
        stripe_event_id="evt_story49_unknown_booking",
        stripe_account_id="acct_story49_unknown_booking",
        stripe_invoice_id="in_story49_unknown_booking",
        paid_at=paid_at,
        metadata={
            "booking_uuid": "BOOK_story49_missing",
            "tid": "story49_unknown_booking_tid",
        },
    )
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

    with Session(_engine()) as session:
        payment_event = session.scalar(
            select(InvoicePaymentEvent).where(
                InvoicePaymentEvent.stripe_event_id == "evt_story49_unknown_booking"
            )
        )

    assert response.status_code == 200
    assert payment_event is not None
    assert payment_event.status == "unmatched"
    assert payment_event.unattributed_reason == UNATTRIBUTED_REASON_UNKNOWN_BOOKING_UUID
    assert payment_event.creator_id == creator.id
    assert payment_event.booking_id is None
    assert payment_event.tid is None


def test_stripe_webhook_repeated_invoice_paid_for_already_paid_invoice_stays_safe():
    invoice = _persist_open_invoice(
        stripe_account_id="acct_story48_already_paid",
        stripe_invoice_id="in_story48_already_paid",
        booking_uuid="BOOK_story48_already_paid",
        tid="story48_tid_already_paid",
    )
    first_paid_at = datetime(2026, 3, 8, 22, 50, tzinfo=timezone.utc)
    second_paid_at = datetime(2026, 3, 8, 22, 55, tzinfo=timezone.utc)
    first_payload = _invoice_paid_payload(
        stripe_event_id="evt_story48_first_paid",
        stripe_account_id="acct_story48_already_paid",
        stripe_invoice_id="in_story48_already_paid",
        paid_at=first_paid_at,
    )
    second_payload = _invoice_paid_payload(
        stripe_event_id="evt_story48_second_paid",
        stripe_account_id="acct_story48_already_paid",
        stripe_invoice_id="in_story48_already_paid",
        paid_at=second_paid_at,
    )
    first_signature_header = _stripe_signature_header(
        payload=first_payload,
        secret=_StubSettings.stripe_webhook_secret,
    )
    second_signature_header = _stripe_signature_header(
        payload=second_payload,
        secret=_StubSettings.stripe_webhook_secret,
    )

    with TestClient(app) as client:
        with _override_app_state("settings", _StubSettings()):
            first_response = client.post(
                "/webhooks/stripe",
                content=first_payload,
                headers={
                    "Content-Type": "application/json",
                    "Stripe-Signature": first_signature_header,
                },
            )
            second_response = client.post(
                "/webhooks/stripe",
                content=second_payload,
                headers={
                    "Content-Type": "application/json",
                    "Stripe-Signature": second_signature_header,
                },
            )

    with Session(_engine()) as session:
        persisted_invoice = session.get(Invoice, invoice.id)
        payment_events = session.scalars(
            select(InvoicePaymentEvent).where(
                InvoicePaymentEvent.stripe_invoice_id == "in_story48_already_paid"
            )
        ).all()

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert persisted_invoice is not None
    assert persisted_invoice.status == "paid"
    assert persisted_invoice.paid_at == first_paid_at
    assert len(payment_events) == 1
    assert payment_events[0].stripe_event_id == "evt_story48_first_paid"
    assert payment_events[0].paid_at == first_paid_at


def test_stripe_webhook_accepts_verified_unsupported_event_type_as_safe_noop():
    invoice = _persist_open_invoice(
        stripe_account_id="acct_story48_noop",
        stripe_invoice_id="in_story48_noop",
        booking_uuid="BOOK_story48_noop",
        tid="story48_tid_noop",
    )
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

    with Session(_engine()) as session:
        persisted_invoice = session.get(Invoice, invoice.id)
        payment_event_count = session.query(InvoicePaymentEvent).count()

    assert response.status_code == 200
    assert response.headers.get("X-Request-Id")
    assert response.json() == {"status": "ok"}
    assert persisted_invoice is not None
    assert persisted_invoice.status == "open"
    assert persisted_invoice.paid_at is None
    assert payment_event_count == 0


def test_stripe_webhook_provider_mismatch_does_not_mutate_paypal_invoice():
    with Session(_engine()) as session:
        creator = Creator(
            name="PP-10 Stripe Mismatch Creator",
            billing_provider="paypal",
            billing_connect_status="connected",
            billing_account_id="merchant_story_pp10_invoice_mismatch",
        )
        session.add(creator)
        session.flush()

        booking_link = BookingLink(
            creator_id=creator.id,
            name="PP-10 Stripe Mismatch Link",
            calendly_url="https://calendly.com/example/story-pp10-stripe-mismatch",
        )
        session.add(booking_link)
        session.flush()

        content = Content(
            creator_id=creator.id,
            booking_link_id=booking_link.id,
            source_url="https://example.com/story-pp10-stripe-mismatch",
            tid="story_pp10_tid_invoice_mismatch",
        )
        session.add(content)
        session.flush()

        booking = Booking(
            creator_id=creator.id,
            tid=content.tid,
            booking_link_id=booking_link.id,
            calendly_booking_uuid="BOOK_story_pp10_invoice_mismatch",
            email="story-pp10-invoice-mismatch@example.com",
            status="created",
            booked_at=datetime(2026, 3, 20, 6, 0, tzinfo=timezone.utc),
        )
        session.add(booking)
        session.flush()

        invoice = Invoice(
            creator_id=creator.id,
            booking_id=booking.id,
            tid=content.tid,
            payment_provider="paypal",
            provider_account_id="merchant_story_pp10_invoice_mismatch",
            provider_invoice_id="INV2_story_pp10_invoice_mismatch",
            amount_cents=19500,
            currency="USD",
            status="open",
            issued_at=datetime(2026, 3, 20, 6, 5, tzinfo=timezone.utc),
        )
        session.add(invoice)
        session.commit()
        session.refresh(invoice)
        invoice_id = invoice.id

    paid_at = datetime(2026, 3, 20, 6, 30, tzinfo=timezone.utc)
    payload = _invoice_paid_payload(
        stripe_event_id="evt_story_pp10_invoice_mismatch",
        stripe_account_id="acct_story_pp10_invoice_mismatch",
        stripe_invoice_id="INV2_story_pp10_invoice_mismatch",
        paid_at=paid_at,
        metadata={"tid": "story_pp10_tid_invoice_mismatch"},
    )
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

    with Session(_engine()) as session:
        persisted_invoice = session.get(Invoice, invoice_id)
        payment_events = session.scalars(
            select(InvoicePaymentEvent).where(
                InvoicePaymentEvent.stripe_event_id == "evt_story_pp10_invoice_mismatch"
            )
        ).all()

    assert response.status_code == 200
    assert persisted_invoice is not None
    assert persisted_invoice.payment_provider == "paypal"
    assert persisted_invoice.status == "open"
    assert persisted_invoice.paid_at is None
    assert len(payment_events) == 1
    assert payment_events[0].status == "unmatched"
    assert payment_events[0].invoice_id is None
    assert payment_events[0].creator_id is None
    assert payment_events[0].booking_id is None
    assert payment_events[0].tid is None
    assert payment_events[0].unattributed_reason == UNATTRIBUTED_REASON_UNKNOWN_STRIPE_INVOICE_ID
