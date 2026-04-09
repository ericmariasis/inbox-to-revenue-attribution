import csv
import hashlib
import hmac
import io
import json
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from time import time

from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import app
from app.services.calendly_webhooks import build_default_calendly_webhook_router
from app.services.email_stub import get_magic_link_outbox
from app.services.stripe_provider import StripeAccountReadiness, StripeInvoiceCreateResult

HTML_ACCEPT_HEADERS = {"Accept": "text/html,application/xhtml+xml"}
SESSION_COOKIE_NAME = "ccp_creator_session"


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

    def build_connect_onboarding_url(self, *, creator_id: str, state: str) -> str:
        self.onboarding_calls.append({"creator_id": creator_id, "state": state})
        return (
            "https://connect.stripe.com/oauth/authorize"
            f"?response_type=code&client_id=ca_test_story54&state={state}&creator_id={creator_id}"
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
        raise AssertionError("Story 54 should not void invoices in the happy-path reporting flow")


def test_phase9_reporting_flow_end_to_end():
    creator_email = f"phase9_creator_{uuid.uuid4().hex}@example.com"
    source_url = "https://example.com/posts/phase9-paid-content"
    settings = get_settings()
    provider = _SequencedStubStripeProvider(
        account_id="acct_story54_connected",
        readiness=StripeAccountReadiness(charges_enabled=True),
        created_invoice_ids=["in_story54_paid"],
    )
    calendly_webhook_router = build_default_calendly_webhook_router(provider=provider)

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
                client.cookies.set(SESSION_COOKIE_NAME, access_token)

                connect_start_response = client.post(
                    "/stripe/connect/start",
                    headers=auth_headers,
                )
                connect_start_payload = connect_start_response.json()
                callback_response = client.get(
                    "/stripe/connect/callback",
                    params={
                        "code": "auth_code_story54",
                        "state": connect_start_payload["state"],
                    },
                )
                me_response = client.get("/me", headers=auth_headers)
                creator_id = me_response.json()["id"]

                booking_link_response = client.post(
                    "/booking-links",
                    headers=auth_headers,
                    json={
                        "name": "Phase 9 Validation Call",
                        "calendly_url": "https://calendly.com/example/phase9-validation-call",
                        "billing_amount_cents": 19500,
                        "billing_currency": " usd ",
                    },
                )
                booking_link = booking_link_response.json()

                content_response = client.post(
                    "/content",
                    headers=auth_headers,
                    json={
                        "source_url": source_url,
                        "booking_link_id": booking_link["id"],
                    },
                )
                content = content_response.json()

                booking_created_payload = _invitee_created_payload(
                    event_id="EVT_story54_paid",
                    calendly_booking_uuid="BOOK_story54_paid",
                    tid=content["tid"],
                    email="phase9-booked@example.com",
                    created_at="2026-03-08T22:00:00Z",
                )
                booking_created_signature = _calendly_signature_header(
                    payload=booking_created_payload,
                    signing_key=settings.calendly_webhook_signing_key,
                )
                booking_created_response = client.post(
                    "/webhooks/calendly",
                    content=booking_created_payload,
                    headers={
                        "Content-Type": "application/json",
                        "Calendly-Webhook-Signature": booking_created_signature,
                    },
                )

                paid_at = datetime(2026, 3, 8, 22, 30, tzinfo=timezone.utc)
                happy_paid_payload = _invoice_paid_payload(
                    stripe_event_id="evt_story54_paid",
                    stripe_account_id=provider.account_id,
                    stripe_invoice_id="in_story54_paid",
                    paid_at=paid_at,
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

                unmatched_paid_payload = _invoice_paid_payload(
                    stripe_event_id="evt_story54_unmatched",
                    stripe_account_id=provider.account_id,
                    stripe_invoice_id="in_story54_unmatched",
                    paid_at=datetime(2026, 3, 8, 22, 45, tzinfo=timezone.utc),
                    metadata={
                        "booking_uuid": "BOOK_story54_missing",
                        "tid": content["tid"],
                    },
                )
                unmatched_paid_signature = _stripe_signature_header(
                    payload=unmatched_paid_payload,
                    secret=settings.stripe_webhook_secret,
                )
                unmatched_paid_response = client.post(
                    "/webhooks/stripe",
                    content=unmatched_paid_payload,
                    headers={
                        "Content-Type": "application/json",
                        "Stripe-Signature": unmatched_paid_signature,
                    },
                )

                reports_response = client.get(
                    "/app/reports",
                    params={"start_date": "2026-03-08", "end_date": "2026-03-08"},
                    headers=HTML_ACCEPT_HEADERS,
                )
                explanation_response = client.get(
                    f"/app/reports/explanations/paid/{content['tid']}",
                    params={"start_date": "2026-03-08", "end_date": "2026-03-08"},
                    headers=HTML_ACCEPT_HEADERS,
                )
                unattributed_response = client.get(
                    "/app/reports/explanations/unattributed",
                    params={"start_date": "2026-03-08", "end_date": "2026-03-08"},
                    headers=HTML_ACCEPT_HEADERS,
                )
                export_response = client.get(
                    "/app/reports/export.csv",
                    params={"start_date": "2026-03-08", "end_date": "2026-03-08"},
                )

                duplicate_happy_paid_response = client.post(
                    "/webhooks/stripe",
                    content=happy_paid_payload,
                    headers={
                        "Content-Type": "application/json",
                        "Stripe-Signature": happy_paid_signature,
                    },
                )
                refreshed_reports_response = client.get(
                    "/app/reports",
                    params={"start_date": "2026-03-08", "end_date": "2026-03-08"},
                    headers=HTML_ACCEPT_HEADERS,
                )

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

    assert content_response.status_code == 201
    assert content["source_url"] == source_url

    assert booking_created_response.status_code == 200
    assert booking_created_response.json() == {"status": "ok"}
    assert happy_paid_response.status_code == 200
    assert happy_paid_response.json() == {"status": "ok"}
    assert unmatched_paid_response.status_code == 200
    assert unmatched_paid_response.json() == {"status": "ok"}

    assert reports_response.status_code == 200
    assert "Reports" in reports_response.text
    assert "195.00" in reports_response.text
    assert "1 paid invoice" in reports_response.text
    assert "1 paid booking" in reports_response.text
    assert source_url in reports_response.text
    assert content["tid"] in reports_response.text
    assert "Why this revenue counted" in reports_response.text
    assert "Why some payments stay outside totals" in reports_response.text
    assert "Unknown booking" in reports_response.text
    assert "Blocked before invoicing" in reports_response.text
    assert "No tracked bookings are blocked before invoicing right now." in reports_response.text
    assert 'href="/app/attention"' in reports_response.text
    assert (
        f'href="/app/reports/explanations/paid/{content["tid"]}?start_date=2026-03-08&amp;end_date=2026-03-08"'
        in reports_response.text
    )
    assert (
        'href="/app/reports/export.csv?start_date=2026-03-08&amp;end_date=2026-03-08"'
        in reports_response.text
    )

    assert explanation_response.status_code == 200
    assert "Why this revenue counted" in explanation_response.text
    assert "Counted in paid totals for this selected window" in explanation_response.text
    assert source_url in explanation_response.text
    assert content["tid"] in explanation_response.text
    assert "BOOK_story54_paid" in explanation_response.text
    assert "in_story54_paid" in explanation_response.text
    assert "evt_story54_paid" in explanation_response.text
    assert "Applied" in explanation_response.text
    assert (
        'href="/app/reports?start_date=2026-03-08&amp;end_date=2026-03-08"'
        in explanation_response.text
    )

    assert unattributed_response.status_code == 200
    assert "Why some payments stay outside totals" in unattributed_response.text
    assert "This page explains what is unresolved right now." in unattributed_response.text
    assert "Unknown booking" in unattributed_response.text
    assert "matching booking is still missing from the current creator-scoped chain" in unattributed_response.text
    assert "Until the booking and invoice chain is clear enough to trust, these events stay outside paid totals, CSV export, and the main paid-results views." in unattributed_response.text
    assert (
        'href="/app/reports?start_date=2026-03-08&amp;end_date=2026-03-08"'
        in unattributed_response.text
    )

    assert export_response.status_code == 200
    assert export_response.headers["content-type"].startswith("text/csv")
    assert (
        export_response.headers["content-disposition"]
        == 'attachment; filename="reports-summary-2026-03-08-to-2026-03-08.csv"'
    )
    csv_rows = list(csv.DictReader(io.StringIO(export_response.text)))
    assert csv_rows == [
        {
            "content_id": content["id"],
            "booking_link_id": booking_link["id"],
            "tid": content["tid"],
            "source_url": source_url,
            "booking_count": "1",
            "paid_revenue_cents": "19500",
            "paid_invoice_count": "1",
            "paid_booking_count": "1",
            "open_blocked_billing_case_count": "0",
            "funnel_status": "paid_result_recorded",
            "first_paid_at": "2026-03-08T22:30:00Z",
            "last_paid_at": "2026-03-08T22:30:00Z",
        }
    ]
    assert "evt_story54_unmatched" not in export_response.text
    assert "Unknown booking" not in export_response.text

    assert duplicate_happy_paid_response.status_code == 200
    assert duplicate_happy_paid_response.json() == {"status": "ok"}
    assert refreshed_reports_response.status_code == 200
    assert "195.00" in refreshed_reports_response.text
    assert "1 paid invoice" in refreshed_reports_response.text
    assert "1 paid booking" in refreshed_reports_response.text

    assert provider.onboarding_calls == [
        {
            "creator_id": creator_id,
            "state": connect_start_payload["state"],
        }
    ]
    assert provider.exchange_calls == [
        {
            "code": "auth_code_story54",
            "state": connect_start_payload["state"],
        }
    ]
    assert provider.readiness_calls == [provider.account_id]
    assert provider.create_calls == [
        {
            "stripe_account_id": provider.account_id,
            "amount_cents": 19500,
            "currency": "USD",
            "metadata": {
                "creator_id": creator_id,
                "booking_provider": "calendly",
                "provider_booking_id": "BOOK_story54_paid",
                "booking_uuid": "BOOK_story54_paid",
                "tid": content["tid"],
            },
            "idempotency_key": "billing:create:calendly:BOOK_story54_paid",
        }
    ]
