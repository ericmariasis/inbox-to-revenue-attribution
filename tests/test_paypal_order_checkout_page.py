import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.main import app
from app.models.invoice import Invoice
from app.models.invoice_payment_event import InvoicePaymentEvent
from app.services.billing_provider import BillingAccountReadiness, BillingProviderError
from app.services.browser_session import SESSION_COOKIE_NAME
from app.services.paypal_provider import PayPalCheckoutCaptureResult, PayPalCheckoutOrderResult

HTML_ACCEPT_HEADERS = {"Accept": "text/html,application/xhtml+xml"}


def _engine():
    return create_engine(os.environ["TEST_DATABASE_URL"])


def _insert_creator_with_booking(*, email: str) -> dict[str, str]:
    creator_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    booking_link_id = str(uuid.uuid4())
    content_id = str(uuid.uuid4())
    booking_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).replace(microsecond=0)

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
                "name": "PayPal Checkout Page Creator",
                "billing_provider": "paypal",
                "billing_connect_status": "connected",
                "billing_account_id": "merchant_story_pp17a",
                "billing_connected_at": now,
                "stripe_connect_status": "pending",
                "stripe_account_id": None,
                "stripe_connected_at": None,
            },
        )
        conn.execute(
            text("INSERT INTO auth_users (id, creator_id, email) VALUES (:id, :creator_id, :email)"),
            {"id": user_id, "creator_id": creator_id, "email": email},
        )
        conn.execute(
            text(
                "INSERT INTO booking_links "
                "(id, creator_id, name, provider, destination_url, calendly_url, billing_amount_cents, billing_currency) "
                "VALUES "
                "(:id, :creator_id, :name, :provider, :destination_url, :calendly_url, :billing_amount_cents, :billing_currency)"
            ),
            {
                "id": booking_link_id,
                "creator_id": creator_id,
                "name": "PayPal Orders Link",
                "provider": "calendly",
                "destination_url": "https://calendly.com/example/paypal-orders",
                "calendly_url": "https://calendly.com/example/paypal-orders",
                "billing_amount_cents": 15000,
                "billing_currency": "USD",
            },
        )
        conn.execute(
            text(
                "INSERT INTO content (id, creator_id, booking_link_id, source_url, tid, created_at, updated_at) "
                "VALUES (:id, :creator_id, :booking_link_id, :source_url, :tid, :created_at, :updated_at)"
            ),
            {
                "id": content_id,
                "creator_id": creator_id,
                "booking_link_id": booking_link_id,
                "source_url": "https://example.com/posts/paypal-order-checkout-page",
                "tid": "paypal_order_checkout_page_tid",
                "created_at": now,
                "updated_at": now,
            },
        )
        conn.execute(
            text(
                "INSERT INTO bookings "
                "(id, creator_id, tid, booking_link_id, provider, provider_booking_id, calendly_booking_uuid, email, status, attribution_status, unattributed_reason, frozen_billing_amount_cents, frozen_billing_currency, booked_at, canceled_at) "
                "VALUES "
                "(:id, :creator_id, :tid, :booking_link_id, :provider, :provider_booking_id, :calendly_booking_uuid, :email, :status, :attribution_status, :unattributed_reason, :frozen_billing_amount_cents, :frozen_billing_currency, :booked_at, :canceled_at)"
            ),
            {
                "id": booking_id,
                "creator_id": creator_id,
                "tid": "paypal_order_checkout_page_tid",
                "booking_link_id": booking_link_id,
                "provider": "calendly",
                "provider_booking_id": "BOOK_story_pp17a_checkout",
                "calendly_booking_uuid": "BOOK_story_pp17a_checkout",
                "email": "buyer@example.com",
                "status": "created",
                "attribution_status": "attributed",
                "unattributed_reason": None,
                "frozen_billing_amount_cents": None,
                "frozen_billing_currency": None,
                "booked_at": now,
                "canceled_at": None,
            },
        )

    return {"creator_id": creator_id, "user_id": user_id, "booking_id": booking_id, "email": email}


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
    marker_name = f"_{name}_overridden"
    had_marker = hasattr(app.state, marker_name)
    previous_marker = getattr(app.state, marker_name, None)
    setattr(app.state, name, value)
    setattr(app.state, marker_name, True)
    try:
        yield
    finally:
        if had_attr:
            setattr(app.state, name, previous_value)
        else:
            delattr(app.state, name)
        if had_marker:
            setattr(app.state, marker_name, previous_marker)
        else:
            delattr(app.state, marker_name)


def _paypal_operator_only_settings(*emails: str, environment: str = "sandbox"):
    settings = get_settings()
    return settings.model_copy(
        update={
            "app_env": "test",
            "paypal_environment": environment,
            "paypal_creator_access": "operator_only",
            "operator_email_allowlist": ",".join(emails),
            "paypal_partner_attribution_id": "BN-story-pp17a",
            "paypal_sandbox_client_id": "sandbox-client-story-pp17a",
            "paypal_live_client_id": "live-client-story-pp17a",
        }
    )


class _StubPayPalProvider:
    def __init__(self, *, capture_error_code: str | None = None):
        self.readiness_calls: list[str] = []
        self.order_calls: list[dict[str, str]] = []
        self.capture_calls: list[dict[str, str]] = []
        self.capture_error_code = capture_error_code

    def get_billing_account_readiness(self, *, provider_account_id: str) -> BillingAccountReadiness:
        self.readiness_calls.append(provider_account_id)
        return BillingAccountReadiness(can_create_invoices=True)

    def create_checkout_order(
        self,
        *,
        provider_account_id: str,
        amount_cents: int,
        currency: str,
        return_url: str,
        cancel_url: str,
        idempotency_key: str,
        custom_id: str | None = None,
        payer_email: str | None = None,
    ) -> PayPalCheckoutOrderResult:
        self.order_calls.append(
            {
                "provider_account_id": provider_account_id,
                "return_url": return_url,
                "cancel_url": cancel_url,
                "idempotency_key": idempotency_key,
                "custom_id": custom_id or "",
                "payer_email": payer_email or "",
            }
        )
        return PayPalCheckoutOrderResult(
            order_id="ORDER-story-pp17a-checkout",
            status="PAYER_ACTION_REQUIRED",
            approval_url="https://www.sandbox.paypal.com/checkoutnow?token=ORDER-story-pp17a-checkout",
        )

    def capture_checkout_order(
        self,
        *,
        provider_account_id: str,
        provider_order_id: str,
        idempotency_key: str,
    ) -> PayPalCheckoutCaptureResult:
        self.capture_calls.append(
            {
                "provider_account_id": provider_account_id,
                "provider_order_id": provider_order_id,
                "idempotency_key": idempotency_key,
            }
        )
        if self.capture_error_code is not None:
            raise BillingProviderError(
                "paypal order capture unavailable",
                provider_name="paypal",
                operation="paypal_order_capture",
                http_status=422,
                error_code=self.capture_error_code,
            )
        return PayPalCheckoutCaptureResult(
            order_id=provider_order_id,
            status="COMPLETED",
            capture_id="CAPTURE-story-pp17a-checkout",
            capture_status="COMPLETED",
            paid_at=datetime(2026, 4, 16, 23, 15, tzinfo=timezone.utc),
        )


def test_paypal_order_checkout_page_renders_sdk_for_allowlisted_operator():
    inserted = _insert_creator_with_booking(
        email=f"paypal_order_checkout_page_{uuid.uuid4().hex}@example.com"
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    provider = _StubPayPalProvider()
    settings = _paypal_operator_only_settings(inserted["email"])

    with _override_app_state("settings", settings):
        with _override_app_state("paypal_provider", provider):
            with TestClient(app) as client:
                client.cookies.set(SESSION_COOKIE_NAME, access_token)
                response = client.get(
                    "/paypal/orders/checkout-page",
                    params={"booking_id": inserted["booking_id"]},
                    headers=HTML_ACCEPT_HEADERS,
                )

    assert response.status_code == 200
    assert "PayPal checkout proof" in response.text
    assert "sandbox-client-story-pp17a" in response.text
    assert "merchant-id=merchant_story_pp17a" in response.text
    assert "buyer-country=US" in response.text
    assert 'data-partner-attribution-id="BN-story-pp17a"' in response.text
    assert f'const bookingId = "{inserted["booking_id"]}";' in response.text
    assert "/paypal/orders/start" in response.text
    assert "/paypal/orders/capture" in response.text
    assert 'layout: "vertical"' in response.text
    assert 'label: "paypal"' in response.text
    assert "Pending until button click" in response.text
    assert 'const orderId = "ORDER-story-pp17a-checkout";' not in response.text
    assert provider.readiness_calls == []
    assert provider.order_calls == []

    with Session(_engine()) as session:
        invoice = session.scalar(select(Invoice).where(Invoice.booking_id == uuid.UUID(inserted["booking_id"])))
        assert invoice is None


def test_paypal_order_checkout_page_hides_for_non_operator_browser_user():
    inserted = _insert_creator_with_booking(
        email=f"paypal_order_checkout_hidden_{uuid.uuid4().hex}@example.com"
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    provider = _StubPayPalProvider()
    settings = _paypal_operator_only_settings("ops@example.com")

    with _override_app_state("settings", settings):
        with _override_app_state("paypal_provider", provider):
            with TestClient(app) as client:
                client.cookies.set(SESSION_COOKIE_NAME, access_token)
                response = client.get(
                    "/paypal/orders/checkout-page",
                    params={"booking_id": inserted["booking_id"]},
                    headers=HTML_ACCEPT_HEADERS,
                )

    assert response.status_code == 404
    assert response.json() == {"detail": "paypal checkout page not found"}
    assert provider.readiness_calls == []
    assert provider.order_calls == []


def test_paypal_order_capture_browser_endpoint_marks_paid_for_allowlisted_operator():
    inserted = _insert_creator_with_booking(
        email=f"paypal_order_capture_browser_{uuid.uuid4().hex}@example.com"
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    provider = _StubPayPalProvider()
    settings = _paypal_operator_only_settings(inserted["email"])

    with _override_app_state("settings", settings):
        with _override_app_state("paypal_provider", provider):
            with TestClient(app) as client:
                client.cookies.set(SESSION_COOKIE_NAME, access_token)
                checkout_page_response = client.get(
                    "/paypal/orders/checkout-page",
                    params={"booking_id": inserted["booking_id"]},
                    headers=HTML_ACCEPT_HEADERS,
                )
                start_response = client.post(
                    "/paypal/orders/start",
                    json={"booking_id": inserted["booking_id"]},
                )
                capture_response = client.post(
                    "/paypal/orders/capture",
                    json={
                        "booking_id": inserted["booking_id"],
                        "provider_order_id": "ORDER-story-pp17a-checkout",
                    },
                )

    assert checkout_page_response.status_code == 200
    assert start_response.status_code == 200
    assert capture_response.status_code == 200
    assert capture_response.json()["outcome"] == "captured"
    assert capture_response.json()["capture_id"] == "CAPTURE-story-pp17a-checkout"
    assert len(provider.order_calls) == 1
    assert provider.order_calls[0]["provider_account_id"] == "merchant_story_pp17a"
    assert "/paypal/orders/callback?state=" in provider.order_calls[0]["return_url"]
    assert "/paypal/orders/callback?state=" in provider.order_calls[0]["cancel_url"]
    assert "cancel=true" in provider.order_calls[0]["cancel_url"]
    assert provider.order_calls[0]["idempotency_key"] == f"paypal:order:start:{inserted['booking_id']}"
    assert provider.order_calls[0]["custom_id"] == inserted["booking_id"]
    assert provider.order_calls[0]["payer_email"] == "buyer@example.com"
    assert provider.capture_calls == [
        {
            "provider_account_id": "merchant_story_pp17a",
            "provider_order_id": "ORDER-story-pp17a-checkout",
            "idempotency_key": "paypal:order:capture:ORDER-story-pp17a-checkout",
        }
    ]

    with Session(_engine()) as session:
        invoice = session.scalar(
            select(Invoice).where(Invoice.booking_id == uuid.UUID(inserted["booking_id"]))
        )
        payment_event = session.scalar(
            select(InvoicePaymentEvent).where(
                InvoicePaymentEvent.payment_provider == "paypal",
                InvoicePaymentEvent.provider_event_id == "CAPTURE-story-pp17a-checkout",
            )
        )
        assert invoice is not None
        assert invoice.status == "paid"
        assert payment_event is not None
        assert payment_event.provider_invoice_id == "ORDER-story-pp17a-checkout"


def test_paypal_order_capture_browser_endpoint_returns_instrument_declined_signal():
    inserted = _insert_creator_with_booking(
        email=f"paypal_order_capture_declined_{uuid.uuid4().hex}@example.com"
    )
    access_token = _access_token(
        user_id=inserted["user_id"],
        creator_id=inserted["creator_id"],
        email=inserted["email"],
        expires_delta=timedelta(hours=24),
    )
    provider = _StubPayPalProvider(capture_error_code="INSTRUMENT_DECLINED")
    settings = _paypal_operator_only_settings(inserted["email"])

    with _override_app_state("settings", settings):
        with _override_app_state("paypal_provider", provider):
            with TestClient(app) as client:
                client.cookies.set(SESSION_COOKIE_NAME, access_token)
                start_response = client.post(
                    "/paypal/orders/start",
                    json={"booking_id": inserted["booking_id"]},
                )
                capture_response = client.post(
                    "/paypal/orders/capture",
                    json={
                        "booking_id": inserted["booking_id"],
                        "provider_order_id": "ORDER-story-pp17a-checkout",
                    },
                )

    assert start_response.status_code == 200
    assert capture_response.status_code == 409
    assert capture_response.json() == {
        "detail": {
            "message": "PayPal asked the buyer to choose another funding source.",
            "error_code": "INSTRUMENT_DECLINED",
        }
    }

    with Session(_engine()) as session:
        invoice = session.scalar(
            select(Invoice).where(Invoice.booking_id == uuid.UUID(inserted["booking_id"]))
        )
        payment_event = session.scalar(
            select(InvoicePaymentEvent).where(
                InvoicePaymentEvent.payment_provider == "paypal",
                InvoicePaymentEvent.provider_event_id == "ORDER-story-pp17a-checkout",
            )
        )
        assert invoice is not None
        assert invoice.status == "open"
        assert payment_event is None
