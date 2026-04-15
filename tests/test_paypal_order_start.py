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
from app.services.billing_provider import BillingAccountReadiness
from app.services.paypal_provider import PayPalCheckoutOrderResult


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
                "name": "PayPal Order Start Creator",
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
                "source_url": "https://example.com/posts/paypal-order-start",
                "tid": "paypal_order_start_tid",
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
                "tid": "paypal_order_start_tid",
                "booking_link_id": booking_link_id,
                "provider": "calendly",
                "provider_booking_id": "BOOK_story_pp17_start",
                "calendly_booking_uuid": "BOOK_story_pp17_start",
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
        }
    )


class _StubPayPalProvider:
    def __init__(self):
        self.readiness_calls: list[str] = []
        self.order_calls: list[dict[str, str]] = []

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
                "amount_cents": str(amount_cents),
                "currency": currency,
                "return_url": return_url,
                "cancel_url": cancel_url,
                "idempotency_key": idempotency_key,
                "custom_id": custom_id or "",
                "payer_email": payer_email or "",
            }
        )
        return PayPalCheckoutOrderResult(
            order_id="ORDER-story-pp17-start",
            status="PAYER_ACTION_REQUIRED",
            approval_url="https://www.sandbox.paypal.com/checkoutnow?token=ORDER-story-pp17-start",
        )


def test_paypal_order_start_returns_approval_url_and_persists_open_invoice():
    inserted = _insert_creator_with_booking(
        email=f"paypal_order_start_{uuid.uuid4().hex}@example.com"
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
                response = client.post(
                    "/paypal/orders/start",
                    json={"booking_id": inserted["booking_id"]},
                    headers={"Authorization": f"Bearer {access_token}"},
                )

    assert response.status_code == 200
    payload = response.json()
    assert payload["provider_order_id"] == "ORDER-story-pp17-start"
    assert payload["approval_url"] == "https://www.sandbox.paypal.com/checkoutnow?token=ORDER-story-pp17-start"
    assert payload["state"]
    assert provider.readiness_calls == ["merchant_story_pp17"]
    assert provider.order_calls[0]["custom_id"] == inserted["booking_id"]
    assert provider.order_calls[0]["payer_email"] == "buyer@example.com"

    with Session(_engine()) as session:
        invoice = session.scalar(select(Invoice).where(Invoice.id == uuid.UUID(payload["invoice_id"])))
        assert invoice is not None
        assert invoice.payment_provider == "paypal"
        assert invoice.provider_invoice_id == "ORDER-story-pp17-start"
        assert (
            invoice.provider_action_url
            == "https://www.sandbox.paypal.com/checkoutnow?token=ORDER-story-pp17-start"
        )
        assert invoice.status == "open"


def test_paypal_order_start_hides_path_for_non_operator_creator():
    inserted = _insert_creator_with_booking(
        email=f"paypal_order_hidden_{uuid.uuid4().hex}@example.com"
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
                response = client.post(
                    "/paypal/orders/start",
                    json={"booking_id": inserted["booking_id"]},
                    headers={"Authorization": f"Bearer {access_token}"},
                )

    assert response.status_code == 404
    assert provider.readiness_calls == []
    assert provider.order_calls == []
