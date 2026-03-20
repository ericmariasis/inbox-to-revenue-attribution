import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone

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
from app.services.invoice_payment_events import UNATTRIBUTED_REASON_UNKNOWN_PROVIDER_INVOICE_ID
from app.services.paypal_provider import PayPalInvoicePaidSnapshot
from app.services.paypal_webhooks import build_default_paypal_webhook_router


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
    paypal_sandbox_webhook_id = "WH_story_pp8_configured"


class _StubPayPalProvider:
    billing_provider_name = "paypal"

    def __init__(
        self,
        *,
        verified: bool = True,
        paid_snapshot: PayPalInvoicePaidSnapshot | None = None,
    ):
        self._verified = verified
        self._paid_snapshot = paid_snapshot or PayPalInvoicePaidSnapshot(
            invoice_id="INV2_story_pp8_default",
            status="PAID",
            payment_type="PAYPAL",
            payment_method="PAYPAL",
            transaction_status="SUCCESS",
            paid_at=datetime(2026, 3, 20, 4, 52, 7, tzinfo=timezone.utc),
        )
        self.verify_calls: list[dict[str, object]] = []
        self.snapshot_calls: list[dict[str, str]] = []

    def verify_webhook_event(
        self,
        *,
        webhook_id: str,
        auth_algo: str,
        cert_url: str,
        transmission_id: str,
        transmission_sig: str,
        transmission_time: str,
        webhook_event,
    ) -> bool:
        self.verify_calls.append(
            {
                "webhook_id": webhook_id,
                "auth_algo": auth_algo,
                "cert_url": cert_url,
                "transmission_id": transmission_id,
                "transmission_sig": transmission_sig,
                "transmission_time": transmission_time,
                "webhook_event": webhook_event,
            }
        )
        return self._verified

    def get_invoice_paid_snapshot(
        self,
        *,
        provider_account_id: str,
        provider_invoice_id: str,
    ) -> PayPalInvoicePaidSnapshot:
        self.snapshot_calls.append(
            {
                "provider_account_id": provider_account_id,
                "provider_invoice_id": provider_invoice_id,
            }
        )
        return self._paid_snapshot


def _engine():
    return create_engine(os.environ["TEST_DATABASE_URL"])


def _paypal_headers() -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "PayPal-Auth-Algo": "SHA256withRSA",
        "PayPal-Cert-Url": "https://api.sandbox.paypal.com/v1/notifications/certs/CERT-story-pp8",
        "PayPal-Transmission-Id": "transmission_story_pp8",
        "PayPal-Transmission-Sig": "sig_story_pp8",
        "PayPal-Transmission-Time": "2026-03-20T04:52:23Z",
    }


def _paypal_invoice_paid_payload(
    *,
    paypal_event_id: str,
    provider_invoice_id: str,
) -> bytes:
    return json.dumps(
        {
            "id": paypal_event_id,
            "event_type": "INVOICING.INVOICE.PAID",
            "create_time": "2026-03-20T04:52:16.277Z",
            "resource": {
                "invoice": {
                    "id": provider_invoice_id,
                    "status": "PAID",
                }
            },
        }
    ).encode("utf-8")


def _persist_open_paypal_invoice(
    *,
    provider_account_id: str,
    provider_invoice_id: str,
    booking_uuid: str,
    tid: str,
) -> Invoice:
    with Session(_engine()) as session:
        creator = Creator(
            name="Story PP-8 PayPal Webhook Creator",
            billing_provider="paypal",
            billing_connect_status="connected",
            billing_account_id=provider_account_id,
        )
        session.add(creator)
        session.flush()

        booking_link = BookingLink(
            creator_id=creator.id,
            name="Story PP-8 PayPal Webhook Link",
            calendly_url="https://calendly.com/example/story-pp8-paypal-webhook",
        )
        session.add(booking_link)
        session.flush()

        content = Content(
            creator_id=creator.id,
            booking_link_id=booking_link.id,
            source_url="https://example.com/story-pp8-paypal-webhook",
            tid=tid,
        )
        session.add(content)
        session.flush()

        booking = Booking(
            creator_id=creator.id,
            tid=content.tid,
            booking_link_id=booking_link.id,
            calendly_booking_uuid=booking_uuid,
            email="story-pp8-booked@example.com",
            status="created",
            booked_at=datetime(2026, 3, 20, 4, 50, tzinfo=timezone.utc),
        )
        session.add(booking)
        session.flush()

        invoice = Invoice(
            creator_id=creator.id,
            booking_id=booking.id,
            tid=content.tid,
            payment_provider="paypal",
            provider_account_id=provider_account_id,
            provider_invoice_id=provider_invoice_id,
            amount_cents=4500,
            currency="USD",
            status="open",
            issued_at=datetime(2026, 3, 20, 4, 50, tzinfo=timezone.utc),
        )
        session.add(invoice)
        session.commit()
        session.refresh(invoice)
        return invoice


def test_paypal_webhook_invoice_paid_marks_matched_paypal_invoice_paid_and_persists_payment_event():
    invoice = _persist_open_paypal_invoice(
        provider_account_id="merchant_story_pp8",
        provider_invoice_id="INV2_story_pp8_matched",
        booking_uuid="BOOK_story_pp8_matched",
        tid="story_pp8_tid_matched",
    )
    paid_at = datetime(2026, 3, 20, 4, 52, 7, tzinfo=timezone.utc)
    provider = _StubPayPalProvider(
        paid_snapshot=PayPalInvoicePaidSnapshot(
            invoice_id="INV2_story_pp8_matched",
            status="PAID",
            payment_type="PAYPAL",
            payment_method="PAYPAL",
            transaction_status="SUCCESS",
            paid_at=paid_at,
        )
    )
    router = build_default_paypal_webhook_router(
        session_factory=lambda: Session(_engine()),
        provider=provider,
        now_fn=lambda: paid_at,
    )
    payload = _paypal_invoice_paid_payload(
        paypal_event_id="WH_story_pp8_matched",
        provider_invoice_id="INV2_story_pp8_matched",
    )

    with TestClient(app) as client:
        with _override_app_state("settings", _StubSettings()):
            with _override_app_state("paypal_provider", provider):
                with _override_app_state("paypal_webhook_router", router):
                    response = client.post(
                        "/webhooks/paypal",
                        content=payload,
                        headers=_paypal_headers(),
                    )

    with Session(_engine()) as session:
        persisted_invoice = session.get(Invoice, invoice.id)
        payment_events = session.scalars(
            select(InvoicePaymentEvent).where(
                InvoicePaymentEvent.payment_provider == "paypal",
                InvoicePaymentEvent.provider_event_id == "WH_story_pp8_matched",
            )
        ).all()

    assert response.status_code == 200
    assert response.headers.get("X-Request-Id")
    assert response.json() == {"status": "ok"}
    assert provider.verify_calls == [
        {
            "webhook_id": "WH_story_pp8_configured",
            "auth_algo": "SHA256withRSA",
            "cert_url": "https://api.sandbox.paypal.com/v1/notifications/certs/CERT-story-pp8",
            "transmission_id": "transmission_story_pp8",
            "transmission_sig": "sig_story_pp8",
            "transmission_time": "2026-03-20T04:52:23Z",
            "webhook_event": json.loads(payload),
        }
    ]
    assert provider.snapshot_calls == [
        {
            "provider_account_id": "merchant_story_pp8",
            "provider_invoice_id": "INV2_story_pp8_matched",
        }
    ]
    assert persisted_invoice is not None
    assert persisted_invoice.status == "paid"
    assert persisted_invoice.paid_at == paid_at
    assert len(payment_events) == 1
    assert payment_events[0].provider_event_type == "INVOICING.INVOICE.PAID"
    assert payment_events[0].provider_account_id == "merchant_story_pp8"
    assert payment_events[0].provider_invoice_id == "INV2_story_pp8_matched"
    assert payment_events[0].stripe_event_id is None
    assert payment_events[0].stripe_event_type is None
    assert payment_events[0].stripe_account_id is None
    assert payment_events[0].stripe_invoice_id is None
    assert payment_events[0].invoice_id == invoice.id
    assert payment_events[0].creator_id == invoice.creator_id
    assert payment_events[0].booking_id == invoice.booking_id
    assert payment_events[0].tid == "story_pp8_tid_matched"
    assert payment_events[0].status == "applied"
    assert payment_events[0].unattributed_reason is None
    assert payment_events[0].paid_at == paid_at
    assert payment_events[0].processed_at == paid_at


def test_paypal_webhook_rejects_failed_verification_without_mutating_invoice():
    invoice = _persist_open_paypal_invoice(
        provider_account_id="merchant_story_pp8_failed_verify",
        provider_invoice_id="INV2_story_pp8_failed_verify",
        booking_uuid="BOOK_story_pp8_failed_verify",
        tid="story_pp8_tid_failed_verify",
    )
    provider = _StubPayPalProvider(verified=False)
    router = build_default_paypal_webhook_router(
        session_factory=lambda: Session(_engine()),
        provider=provider,
    )
    payload = _paypal_invoice_paid_payload(
        paypal_event_id="WH_story_pp8_failed_verify",
        provider_invoice_id="INV2_story_pp8_failed_verify",
    )

    with TestClient(app) as client:
        with _override_app_state("settings", _StubSettings()):
            with _override_app_state("paypal_provider", provider):
                with _override_app_state("paypal_webhook_router", router):
                    response = client.post(
                        "/webhooks/paypal",
                        content=payload,
                        headers=_paypal_headers(),
                    )

    with Session(_engine()) as session:
        persisted_invoice = session.get(Invoice, invoice.id)
        payment_event_count = session.query(InvoicePaymentEvent).count()

    assert response.status_code == 400
    assert response.headers.get("X-Request-Id")
    assert response.json() == {"detail": "invalid paypal webhook verification"}
    assert len(provider.verify_calls) == 1
    assert provider.snapshot_calls == []
    assert persisted_invoice is not None
    assert persisted_invoice.status == "open"
    assert persisted_invoice.paid_at is None
    assert payment_event_count == 0


def test_paypal_webhook_verified_unmatched_invoice_persists_unmatched_payment_event():
    provider = _StubPayPalProvider()
    router = build_default_paypal_webhook_router(
        session_factory=lambda: Session(_engine()),
        provider=provider,
    )
    payload = _paypal_invoice_paid_payload(
        paypal_event_id="WH_story_pp8_unmatched",
        provider_invoice_id="INV2_story_pp8_unmatched",
    )

    with TestClient(app) as client:
        with _override_app_state("settings", _StubSettings()):
            with _override_app_state("paypal_provider", provider):
                with _override_app_state("paypal_webhook_router", router):
                    response = client.post(
                        "/webhooks/paypal",
                        content=payload,
                        headers=_paypal_headers(),
                    )

    with Session(_engine()) as session:
        payment_events = session.scalars(
            select(InvoicePaymentEvent).where(
                InvoicePaymentEvent.payment_provider == "paypal",
                InvoicePaymentEvent.provider_event_id == "WH_story_pp8_unmatched",
            )
        ).all()

    assert response.status_code == 200
    assert provider.snapshot_calls == []
    assert len(payment_events) == 1
    assert payment_events[0].status == "unmatched"
    assert payment_events[0].provider_account_id is None
    assert payment_events[0].provider_invoice_id == "INV2_story_pp8_unmatched"
    assert payment_events[0].creator_id is None
    assert payment_events[0].booking_id is None
    assert payment_events[0].tid is None
    assert payment_events[0].unattributed_reason == UNATTRIBUTED_REASON_UNKNOWN_PROVIDER_INVOICE_ID


def test_paypal_webhook_verified_control_shape_keeps_invoice_open_without_payment_event():
    invoice = _persist_open_paypal_invoice(
        provider_account_id="merchant_story_pp8_control",
        provider_invoice_id="INV2_story_pp8_control",
        booking_uuid="BOOK_story_pp8_control",
        tid="story_pp8_tid_control",
    )
    provider = _StubPayPalProvider(
        paid_snapshot=PayPalInvoicePaidSnapshot(
            invoice_id="INV2_story_pp8_control",
            status="MARKED_AS_PAID",
            payment_type="EXTERNAL",
            payment_method="OTHER",
            transaction_status=None,
            paid_at=datetime(2026, 3, 20, 4, 53, 13, tzinfo=timezone.utc),
        )
    )
    router = build_default_paypal_webhook_router(
        session_factory=lambda: Session(_engine()),
        provider=provider,
    )
    payload = _paypal_invoice_paid_payload(
        paypal_event_id="WH_story_pp8_control",
        provider_invoice_id="INV2_story_pp8_control",
    )

    with TestClient(app) as client:
        with _override_app_state("settings", _StubSettings()):
            with _override_app_state("paypal_provider", provider):
                with _override_app_state("paypal_webhook_router", router):
                    response = client.post(
                        "/webhooks/paypal",
                        content=payload,
                        headers=_paypal_headers(),
                    )

    with Session(_engine()) as session:
        persisted_invoice = session.get(Invoice, invoice.id)
        payment_events = session.scalars(
            select(InvoicePaymentEvent).where(
                InvoicePaymentEvent.payment_provider == "paypal",
                InvoicePaymentEvent.provider_event_id == "WH_story_pp8_control",
            )
        ).all()

    assert response.status_code == 200
    assert provider.snapshot_calls == [
        {
            "provider_account_id": "merchant_story_pp8_control",
            "provider_invoice_id": "INV2_story_pp8_control",
        }
    ]
    assert persisted_invoice is not None
    assert persisted_invoice.status == "open"
    assert persisted_invoice.paid_at is None
    assert payment_events == []
