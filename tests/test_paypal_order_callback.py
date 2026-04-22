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
from app.services.billing_provider import BillingAccountReadiness
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
                "name": "PayPal Order Callback Creator",
                "billing_provider": "paypal",
                "billing_connect_status": "connected",
                "billing_account_id": "merchant_story_pp17",
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
                "source_url": "https://example.com/posts/paypal-order-callback",
                "tid": "paypal_order_callback_tid",
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
                "tid": "paypal_order_callback_tid",
                "booking_link_id": booking_link_id,
                "provider": "calendly",
                "provider_booking_id": "BOOK_story_pp17_callback",
                "calendly_booking_uuid": "BOOK_story_pp17_callback",
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


def _shipping_address_payload() -> dict[str, str]:
    return {
        "full_name": "Buyer Example",
        "address_line_1": "123 Main St",
        "address_line_2": "Apt 5",
        "city": "San Jose",
        "state_or_region": "CA",
        "postal_code": "95131",
        "country_code": "US",
    }


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
        }
    )


class _StubPayPalProvider:
    def __init__(self):
        self.readiness_calls: list[str] = []
        self.order_calls: list[dict[str, str]] = []
        self.capture_calls: list[dict[str, str]] = []

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
        shipping_address,
    ) -> PayPalCheckoutOrderResult:
        self.order_calls.append(
            {
                "provider_account_id": provider_account_id,
                "return_url": return_url,
                "cancel_url": cancel_url,
                "idempotency_key": idempotency_key,
                "shipping_country_code": shipping_address.country_code,
            }
        )
        return PayPalCheckoutOrderResult(
            order_id="ORDER-story-pp17-callback",
            status="PAYER_ACTION_REQUIRED",
            approval_url="https://www.sandbox.paypal.com/checkoutnow?token=ORDER-story-pp17-callback",
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
        return PayPalCheckoutCaptureResult(
            order_id=provider_order_id,
            status="COMPLETED",
            capture_id="CAPTURE-story-pp17-callback",
            capture_status="COMPLETED",
            paid_at=datetime(2026, 4, 14, 20, 52, 7, tzinfo=timezone.utc),
        )


def test_paypal_order_callback_captures_paid_order_and_returns_browser_success_page():
    inserted = _insert_creator_with_booking(
        email=f"paypal_order_callback_{uuid.uuid4().hex}@example.com"
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
                start_response = client.post(
                    "/paypal/orders/start",
                    json={
                        "booking_id": inserted["booking_id"],
                        "shipping_address": _shipping_address_payload(),
                    },
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                state = start_response.json()["state"]
                callback_response = client.get(
                    "/paypal/orders/callback",
                    params={
                        "state": state,
                        "token": "ORDER-story-pp17-callback",
                    },
                    headers=HTML_ACCEPT_HEADERS,
                )

    assert start_response.status_code == 200
    assert callback_response.status_code == 200
    assert "PayPal payment completed" in callback_response.text
    assert "ORDER-story-pp17-callback" in callback_response.text
    assert provider.capture_calls == [
        {
            "provider_account_id": "merchant_story_pp17",
            "provider_order_id": "ORDER-story-pp17-callback",
            "idempotency_key": "paypal:order:capture:ORDER-story-pp17-callback",
        }
    ]

    with Session(_engine()) as session:
        invoice = session.scalar(
            select(Invoice).where(Invoice.booking_id == uuid.UUID(inserted["booking_id"]))
        )
        payment_event = session.scalar(
            select(InvoicePaymentEvent).where(
                InvoicePaymentEvent.payment_provider == "paypal",
                InvoicePaymentEvent.provider_event_id == "CAPTURE-story-pp17-callback",
            )
        )
        assert invoice is not None
        assert invoice.status == "paid"
        assert payment_event is not None
        assert payment_event.provider_invoice_id == "ORDER-story-pp17-callback"


def test_paypal_order_callback_rejects_token_mismatch_without_capture():
    inserted = _insert_creator_with_booking(
        email=f"paypal_order_callback_invalid_{uuid.uuid4().hex}@example.com"
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
                start_response = client.post(
                    "/paypal/orders/start",
                    json={
                        "booking_id": inserted["booking_id"],
                        "shipping_address": _shipping_address_payload(),
                    },
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                state = start_response.json()["state"]
                callback_response = client.get(
                    "/paypal/orders/callback",
                    params={
                        "state": state,
                        "token": "ORDER-story-pp17-wrong",
                    },
                )

    assert start_response.status_code == 200
    assert callback_response.status_code == 400
    assert callback_response.json() == {"detail": "invalid paypal order callback"}
    assert provider.capture_calls == []
