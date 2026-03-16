import hashlib
import hmac
import json
import re
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from time import time
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import app
from app.services.calendly_webhooks import build_default_calendly_webhook_router
from app.services.email_stub import get_magic_link_outbox
from app.services.stripe_provider import (
    StripeAccountReadiness,
    StripeInvoiceCreateResult,
    StripeProviderError,
)

HTML_ACCEPT_HEADERS = {"Accept": "text/html,application/xhtml+xml"}


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


def _option_value_for_label(*, rendered_html: str, label: str) -> str:
    match = re.search(
        rf'<option value="([^"]+)"(?: selected)?>{re.escape(label)}</option>',
        rendered_html,
    )
    if match is None:
        raise AssertionError(f"No option found for label {label!r}")
    return match.group(1)


def _blocked_retry_case_id(rendered_html: str) -> str:
    match = re.search(r'/app/attention/blocked-billing/([0-9a-f-]+)/retry', rendered_html)
    if match is None:
        raise AssertionError("No blocked billing retry action found in attention page")
    return match.group(1)


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
        create_responses: list[StripeInvoiceCreateResult | Exception],
    ):
        self.account_id = account_id
        self.readiness = readiness
        self._create_responses = list(create_responses)
        self.onboarding_calls: list[dict[str, str]] = []
        self.exchange_calls: list[dict[str, str]] = []
        self.readiness_calls: list[str] = []
        self.create_calls: list[dict[str, object]] = []

    def build_connect_onboarding_url(self, *, creator_id: str, state: str) -> str:
        self.onboarding_calls.append({"creator_id": creator_id, "state": state})
        return (
            "https://connect.stripe.com/oauth/authorize"
            f"?response_type=code&client_id=ca_test_story60&state={state}&creator_id={creator_id}"
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
        if not self._create_responses:
            raise AssertionError("No stubbed Stripe create response remaining")

        self.create_calls.append(
            {
                "stripe_account_id": stripe_account_id,
                "amount_cents": amount_cents,
                "currency": currency,
                "metadata": metadata,
                "idempotency_key": idempotency_key,
            }
        )

        response = self._create_responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def void_invoice(self, *, stripe_account_id: str, stripe_invoice_id: str) -> None:
        raise AssertionError("Story 60 should not void invoices in the closeout validation flow")


def test_phase10_5_self_serve_trust_flow_end_to_end():
    creator_email = f"phase10_5_creator_{uuid.uuid4().hex}@example.com"
    booking_link_name = "Phase 10.5 Validation Call"
    source_url = "https://example.com/posts/phase10-5-validation-content"
    settings = get_settings()
    provider = _SequencedStubStripeProvider(
        account_id="acct_story60_connected",
        readiness=StripeAccountReadiness(charges_enabled=True),
        create_responses=[
            StripeInvoiceCreateResult(stripe_invoice_id="in_story60_paid", status="open"),
            StripeProviderError(
                "stripe invoice creation failed",
                operation="stripe_invoice_create",
                http_status=502,
                error_code="api_connection_error",
            ),
            StripeInvoiceCreateResult(stripe_invoice_id="in_story60_recovered", status="open"),
        ],
    )
    calendly_webhook_router = build_default_calendly_webhook_router(provider=provider)

    with _override_app_state("stripe_provider", provider):
        with _override_app_state("calendly_webhook_router", calendly_webhook_router):
            with TestClient(app) as client:
                sign_in_response = client.post(
                    "/sign-in",
                    data={"email": creator_email},
                    headers=HTML_ACCEPT_HEADERS,
                    follow_redirects=False,
                )
                first_token = _latest_magic_link_token_for_email(creator_email)
                first_verify_response = client.get(
                    "/auth/magic-link/verify",
                    params={"token": first_token},
                    headers=HTML_ACCEPT_HEADERS,
                    follow_redirects=False,
                )
                initial_home_response = client.get("/app", headers=HTML_ACCEPT_HEADERS)

                invalid_link_response = client.get(
                    "/auth/magic-link/verify",
                    params={"token": first_token},
                    headers=HTML_ACCEPT_HEADERS,
                    follow_redirects=False,
                )
                invalid_link_page_response = client.get(
                    invalid_link_response.headers["location"],
                    headers=HTML_ACCEPT_HEADERS,
                )

                second_sign_in_response = client.post(
                    "/sign-in",
                    data={"email": creator_email},
                    headers=HTML_ACCEPT_HEADERS,
                    follow_redirects=False,
                )
                second_token = _latest_magic_link_token_for_email(creator_email)
                second_verify_response = client.get(
                    "/auth/magic-link/verify",
                    params={"token": second_token},
                    headers=HTML_ACCEPT_HEADERS,
                    follow_redirects=False,
                )

                create_booking_link_response = client.post(
                    "/app/booking-links",
                    data={
                        "name": booking_link_name,
                        "calendly_url": "https://calendly.com/example/phase10-5-validation",
                        "billing_amount_cents": "19500",
                        "billing_currency": " usd ",
                    },
                    headers=HTML_ACCEPT_HEADERS,
                    follow_redirects=False,
                )
                booking_links_page_response = client.get(
                    create_booking_link_response.headers["location"],
                    headers=HTML_ACCEPT_HEADERS,
                )
                booking_link_setup_response = client.get("/app", headers=HTML_ACCEPT_HEADERS)

                content_page_response = client.get("/app/content", headers=HTML_ACCEPT_HEADERS)
                booking_link_id = _option_value_for_label(
                    rendered_html=content_page_response.text,
                    label=booking_link_name,
                )
                create_content_response = client.post(
                    "/app/content",
                    data={
                        "source_url": source_url,
                        "booking_link_id": booking_link_id,
                    },
                    headers=HTML_ACCEPT_HEADERS,
                    follow_redirects=False,
                )
                created_tid = parse_qs(
                    urlparse(create_content_response.headers["location"]).query
                )["tid"][0]
                content_success_page_response = client.get(
                    create_content_response.headers["location"],
                    headers=HTML_ACCEPT_HEADERS,
                )
                content_setup_response = client.get("/app", headers=HTML_ACCEPT_HEADERS)

                connect_start_response = client.post(
                    "/app/stripe/connect/start",
                    headers=HTML_ACCEPT_HEADERS,
                    follow_redirects=False,
                )
                connect_state = parse_qs(
                    urlparse(connect_start_response.headers["location"]).query
                )["state"][0]
                connect_callback_response = client.get(
                    "/stripe/connect/callback",
                    params={
                        "code": "auth_code_story60",
                        "state": connect_state,
                    },
                    headers=HTML_ACCEPT_HEADERS,
                    follow_redirects=False,
                )
                setup_complete_response = client.get("/app", headers=HTML_ACCEPT_HEADERS)

                paid_booking_payload = _invitee_created_payload(
                    event_id="EVT_story60_paid",
                    calendly_booking_uuid="BOOK_story60_paid",
                    tid=created_tid,
                    email="phase10-5-paid@example.com",
                    created_at="2026-03-09T14:00:00Z",
                )
                paid_booking_signature = _calendly_signature_header(
                    payload=paid_booking_payload,
                    signing_key=settings.calendly_webhook_signing_key,
                )
                paid_booking_response = client.post(
                    "/webhooks/calendly",
                    content=paid_booking_payload,
                    headers={
                        "Content-Type": "application/json",
                        "Calendly-Webhook-Signature": paid_booking_signature,
                    },
                )

                blocked_booking_payload = _invitee_created_payload(
                    event_id="EVT_story60_blocked",
                    calendly_booking_uuid="BOOK_story60_blocked",
                    tid=created_tid,
                    email="phase10-5-blocked@example.com",
                    created_at="2026-03-09T14:10:00Z",
                )
                blocked_booking_signature = _calendly_signature_header(
                    payload=blocked_booking_payload,
                    signing_key=settings.calendly_webhook_signing_key,
                )
                blocked_booking_response = client.post(
                    "/webhooks/calendly",
                    content=blocked_booking_payload,
                    headers={
                        "Content-Type": "application/json",
                        "Calendly-Webhook-Signature": blocked_booking_signature,
                    },
                )

                happy_paid_payload = _invoice_paid_payload(
                    stripe_event_id="evt_story60_paid",
                    stripe_account_id=provider.account_id,
                    stripe_invoice_id="in_story60_paid",
                    paid_at=datetime(2026, 3, 9, 14, 30, tzinfo=timezone.utc),
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
                    stripe_event_id="evt_story60_unmatched",
                    stripe_account_id=provider.account_id,
                    stripe_invoice_id="in_story60_unmatched",
                    paid_at=datetime(2026, 3, 9, 14, 45, tzinfo=timezone.utc),
                    metadata={
                        "booking_uuid": "BOOK_story60_missing",
                        "tid": created_tid,
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

                trust_home_response = client.get("/app", headers=HTML_ACCEPT_HEADERS)
                reports_response = client.get(
                    "/app/reports",
                    params={"start_date": "2026-03-09", "end_date": "2026-03-09"},
                    headers=HTML_ACCEPT_HEADERS,
                )
                attention_response = client.get("/app/attention", headers=HTML_ACCEPT_HEADERS)
                blocked_case_id = _blocked_retry_case_id(attention_response.text)

                retry_response = client.post(
                    f"/app/attention/blocked-billing/{blocked_case_id}/retry",
                    headers=HTML_ACCEPT_HEADERS,
                    follow_redirects=False,
                )
                recovered_attention_response = client.get(
                    retry_response.headers["location"],
                    headers=HTML_ACCEPT_HEADERS,
                )
                recovered_reports_response = client.get(
                    "/app/reports",
                    params={"start_date": "2026-03-09", "end_date": "2026-03-09"},
                    headers=HTML_ACCEPT_HEADERS,
                )

    assert sign_in_response.status_code == 303
    assert sign_in_response.headers["location"] == "/sign-in?status=sent"
    assert first_token not in sign_in_response.headers["location"]

    assert first_verify_response.status_code == 303
    assert first_verify_response.headers["location"] == "/app"
    assert first_token not in first_verify_response.text
    assert "ccp_creator_session=" in first_verify_response.headers["set-cookie"]

    assert initial_home_response.status_code == 200
    assert "Setup Home" in initial_home_response.text
    assert "0 of 4 setup steps done" in initial_home_response.text
    assert "Finish Stripe setup" in initial_home_response.text
    assert "Blocked billing and unresolved payments will appear" in initial_home_response.text

    assert invalid_link_response.status_code == 303
    assert invalid_link_response.headers["location"] == "/sign-in?status=invalid-link"
    assert "Max-Age=0" in invalid_link_response.headers["set-cookie"]

    assert invalid_link_page_response.status_code == 200
    assert "That sign-in link is invalid or expired" in invalid_link_page_response.text
    assert "different device or browser than the one where sign-in started" in invalid_link_page_response.text

    assert second_sign_in_response.status_code == 303
    assert second_sign_in_response.headers["location"] == "/sign-in?status=sent"

    assert second_verify_response.status_code == 303
    assert second_verify_response.headers["location"] == "/app"
    assert second_token != first_token

    assert create_booking_link_response.status_code == 303
    assert create_booking_link_response.headers["location"] == "/app/booking-links?status=created"

    assert booking_links_page_response.status_code == 200
    assert "Booking link saved" in booking_links_page_response.text
    assert booking_link_name in booking_links_page_response.text
    assert "Ready for invoice defaults: USD 195.00" in booking_links_page_response.text

    assert booking_link_setup_response.status_code == 200
    assert "2 of 4 setup steps done" in booking_link_setup_response.text
    assert "Finish Stripe setup" in booking_link_setup_response.text
    assert "Billing-ready links" in booking_link_setup_response.text

    assert content_page_response.status_code == 200
    assert 'action="/app/content"' in content_page_response.text

    assert create_content_response.status_code == 303
    assert create_content_response.headers["location"] == f"/app/content?status=created&tid={created_tid}"

    assert content_success_page_response.status_code == 200
    assert "Tracked link ready" in content_success_page_response.text
    assert source_url in content_success_page_response.text
    assert created_tid in content_success_page_response.text

    assert content_setup_response.status_code == 200
    assert "3 of 4 setup steps done" in content_setup_response.text
    assert "Finish Stripe setup" in content_setup_response.text

    assert connect_start_response.status_code == 303
    assert connect_start_response.headers["location"].startswith(
        "https://connect.stripe.com/oauth/authorize"
    )
    assert connect_callback_response.status_code == 303
    assert connect_callback_response.headers["location"] == "/app"

    assert setup_complete_response.status_code == 200
    assert "4 of 4 setup steps done" in setup_complete_response.text
    assert "This workspace is ready to track and waiting for the first paid result." in setup_complete_response.text
    assert "Ready to track and waiting for first paid result" in setup_complete_response.text
    assert "Waiting for first paid result" in setup_complete_response.text
    assert "Connected account" in setup_complete_response.text
    assert provider.account_id in setup_complete_response.text

    assert paid_booking_response.status_code == 200
    assert paid_booking_response.json() == {"status": "ok"}
    assert blocked_booking_response.status_code == 200
    assert blocked_booking_response.json() == {"status": "ok"}
    assert happy_paid_response.status_code == 200
    assert happy_paid_response.json() == {"status": "ok"}
    assert unmatched_paid_response.status_code == 200
    assert unmatched_paid_response.json() == {"status": "ok"}

    assert trust_home_response.status_code == 200
    assert "4 of 4 setup steps done" in trust_home_response.text
    assert "Review attention items" in trust_home_response.text
    assert "Review 2 attention items" in trust_home_response.text
    assert 'href="/app/attention"' in trust_home_response.text

    assert reports_response.status_code == 200
    assert "Reports" in reports_response.text
    assert "195.00" in reports_response.text
    assert "1 paid invoice" in reports_response.text
    assert "1 paid booking" in reports_response.text
    assert source_url in reports_response.text
    assert created_tid in reports_response.text
    assert "Why this revenue counted" in reports_response.text
    assert "Why some payments are not counted yet" in reports_response.text
    assert "Unknown booking" in reports_response.text
    assert "1 booking still blocked before invoicing and outside paid totals." in reports_response.text
    assert 'href="/app/attention"' in reports_response.text

    assert attention_response.status_code == 200
    assert "Attention" in attention_response.text
    assert "BOOK_story60_blocked" in attention_response.text
    assert "Provider error" in attention_response.text
    assert "api_connection_error" in attention_response.text
    assert "Retry invoice creation" in attention_response.text
    assert "evt_story60_unmatched" in attention_response.text
    assert "in_story60_unmatched" in attention_response.text
    assert "Unknown booking" in attention_response.text

    assert retry_response.status_code == 303
    assert retry_response.headers["location"] == "/app/attention?status=recovered"

    assert recovered_attention_response.status_code == 200
    assert "Invoice recovered." in recovered_attention_response.text
    assert "BOOK_story60_blocked" not in recovered_attention_response.text
    assert "No blocked billing cases are waiting right now" in recovered_attention_response.text
    assert "evt_story60_unmatched" in recovered_attention_response.text

    assert recovered_reports_response.status_code == 200
    assert "195.00" in recovered_reports_response.text
    assert "1 paid invoice" in recovered_reports_response.text
    assert "1 booking still blocked before invoicing and outside paid totals." not in recovered_reports_response.text
    assert "No tracked bookings are blocked before invoicing right now." in recovered_reports_response.text

    assert provider.onboarding_calls == [{"creator_id": provider.onboarding_calls[0]["creator_id"], "state": connect_state}]
    assert provider.exchange_calls == [{"code": "auth_code_story60", "state": connect_state}]
    assert provider.readiness_calls == [provider.account_id, provider.account_id, provider.account_id]
    assert provider.create_calls == [
        {
            "stripe_account_id": provider.account_id,
            "amount_cents": 19500,
            "currency": "USD",
            "metadata": {
                "creator_id": provider.onboarding_calls[0]["creator_id"],
                "booking_uuid": "BOOK_story60_paid",
                "tid": created_tid,
            },
            "idempotency_key": "billing:create:BOOK_story60_paid",
        },
        {
            "stripe_account_id": provider.account_id,
            "amount_cents": 19500,
            "currency": "USD",
            "metadata": {
                "creator_id": provider.onboarding_calls[0]["creator_id"],
                "booking_uuid": "BOOK_story60_blocked",
                "tid": created_tid,
            },
            "idempotency_key": "billing:create:BOOK_story60_blocked",
        },
        {
            "stripe_account_id": provider.account_id,
            "amount_cents": 19500,
            "currency": "USD",
            "metadata": {
                "creator_id": provider.onboarding_calls[0]["creator_id"],
                "booking_uuid": "BOOK_story60_blocked",
                "tid": created_tid,
            },
            "idempotency_key": "billing:create:BOOK_story60_blocked",
        },
    ]
