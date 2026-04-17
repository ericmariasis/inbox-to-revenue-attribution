import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from app.models.blocked_billing_case import BlockedBillingCase
from app.models.invoice import Invoice
from app.models.invoice_payment_event import InvoicePaymentEvent
from app.services.billing_provider import BillingAccountReadiness
from app.services.paypal_orders import (
    PAYPAL_ORDER_FLOW_REASON_PROVIDER_ERROR,
    PayPalOrderFlowError,
    PayPalOrdersService,
)
from app.services.paypal_provider import (
    PayPalCheckoutCaptureResult,
    PayPalCheckoutOrderResult,
    PayPalProviderError,
)


def _engine():
    return create_engine(os.environ["TEST_DATABASE_URL"])


def _insert_paypal_creator_with_booking(*, email: str) -> dict[str, str]:
    creator_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    booking_link_id = str(uuid.uuid4())
    content_id = str(uuid.uuid4())
    booking_id = str(uuid.uuid4())
    booked_at = datetime.now(timezone.utc).replace(microsecond=0)

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
                "name": "PayPal Orders Creator",
                "billing_provider": "paypal",
                "billing_connect_status": "connected",
                "billing_account_id": "merchant_story_pp17",
                "billing_connected_at": booked_at,
                "stripe_connect_status": "pending",
                "stripe_account_id": None,
                "stripe_connected_at": None,
            },
        )
        conn.execute(
            text(
                "INSERT INTO auth_users (id, creator_id, email) VALUES (:id, :creator_id, :email)"
            ),
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
                "name": "PayPal Order Link",
                "provider": "calendly",
                "destination_url": "https://calendly.com/example/paypal-order",
                "calendly_url": "https://calendly.com/example/paypal-order",
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
                "source_url": "https://example.com/posts/paypal-order",
                "tid": "paypal_order_tid",
                "created_at": booked_at,
                "updated_at": booked_at,
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
                "tid": "paypal_order_tid",
                "booking_link_id": booking_link_id,
                "provider": "calendly",
                "provider_booking_id": "BOOK_story_pp17",
                "calendly_booking_uuid": "BOOK_story_pp17",
                "email": "buyer@example.com",
                "status": "created",
                "attribution_status": "attributed",
                "unattributed_reason": None,
                "frozen_billing_amount_cents": None,
                "frozen_billing_currency": None,
                "booked_at": booked_at,
                "canceled_at": None,
            },
        )

    return {
        "creator_id": creator_id,
        "booking_id": booking_id,
        "booked_at": booked_at.isoformat(),
    }


class _StubPayPalOrdersProvider:
    def __init__(
        self,
        *,
        readiness: BillingAccountReadiness | None = None,
        created_order: PayPalCheckoutOrderResult | None = None,
        captured_order: PayPalCheckoutCaptureResult | None = None,
        create_error: PayPalProviderError | None = None,
    ):
        self.readiness = readiness or BillingAccountReadiness(can_create_invoices=True)
        self.created_order = created_order
        self.captured_order = captured_order
        self.create_error = create_error
        self.readiness_calls: list[str] = []
        self.create_calls: list[dict[str, str]] = []
        self.capture_calls: list[dict[str, str]] = []

    def get_billing_account_readiness(self, *, provider_account_id: str) -> BillingAccountReadiness:
        self.readiness_calls.append(provider_account_id)
        return self.readiness

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
        self.create_calls.append(
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
        if self.create_error is not None:
            raise self.create_error
        if self.created_order is None:
            raise AssertionError("created_order must be configured")
        return self.created_order

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
        if self.captured_order is None:
            raise AssertionError("captured_order must be configured")
        return self.captured_order


def test_start_order_persists_open_paypal_invoice_with_approval_url():
    fixture = _insert_paypal_creator_with_booking(
        email=f"paypal_orders_start_{uuid.uuid4().hex}@example.com"
    )
    provider = _StubPayPalOrdersProvider(
        created_order=PayPalCheckoutOrderResult(
            order_id="ORDER-story-pp17",
            status="PAYER_ACTION_REQUIRED",
            approval_url="https://www.sandbox.paypal.com/checkoutnow?token=ORDER-story-pp17",
        )
    )
    service = PayPalOrdersService(
        session_factory=lambda: Session(_engine()),
        provider=provider,
    )

    result = service.start_order(
        creator_id=uuid.UUID(fixture["creator_id"]),
        booking_id=uuid.UUID(fixture["booking_id"]),
    )

    assert result.outcome == "created"
    assert result.provider_order_id == "ORDER-story-pp17"
    assert result.approval_url == "https://www.sandbox.paypal.com/checkoutnow?token=ORDER-story-pp17"
    assert provider.readiness_calls == ["merchant_story_pp17"]
    assert provider.create_calls[0]["provider_account_id"] == "merchant_story_pp17"
    assert provider.create_calls[0]["amount_cents"] == "15000"
    assert provider.create_calls[0]["currency"] == "USD"
    assert provider.create_calls[0]["custom_id"] == fixture["booking_id"]
    assert provider.create_calls[0]["payer_email"] == "buyer@example.com"

    with Session(_engine()) as session:
        invoice = session.scalar(select(Invoice).where(Invoice.id == result.invoice_id))
        assert invoice is not None
        assert invoice.payment_provider == "paypal"
        assert invoice.provider_account_id == "merchant_story_pp17"
        assert invoice.provider_invoice_id == "ORDER-story-pp17"
        assert (
            invoice.provider_action_url
            == "https://www.sandbox.paypal.com/checkoutnow?token=ORDER-story-pp17"
        )
        assert invoice.status == "open"
        assert invoice.amount_cents == 15000
        assert invoice.currency == "USD"


def test_start_order_records_blocked_case_when_paypal_provider_create_fails():
    fixture = _insert_paypal_creator_with_booking(
        email=f"paypal_orders_blocked_{uuid.uuid4().hex}@example.com"
    )
    provider = _StubPayPalOrdersProvider(
        create_error=PayPalProviderError(
            "paypal order creation failed",
            operation="paypal_order_create",
            http_status=502,
            error_code="INTERNAL_SERVER_ERROR",
        )
    )
    service = PayPalOrdersService(
        session_factory=lambda: Session(_engine()),
        provider=provider,
    )

    with pytest.raises(PayPalOrderFlowError) as exc_info:
        service.start_order(
            creator_id=uuid.UUID(fixture["creator_id"]),
            booking_id=uuid.UUID(fixture["booking_id"]),
        )

    assert exc_info.value.reason_code == PAYPAL_ORDER_FLOW_REASON_PROVIDER_ERROR
    assert exc_info.value.status_code == 503
    assert exc_info.value.provider_operation == "paypal_order_create"
    assert exc_info.value.provider_http_status == 502
    assert exc_info.value.provider_error_code == "INTERNAL_SERVER_ERROR"

    with Session(_engine()) as session:
        invoice = session.scalar(
            select(Invoice).where(Invoice.booking_id == uuid.UUID(fixture["booking_id"]))
        )
        blocked_case = session.scalar(
            select(BlockedBillingCase).where(
                BlockedBillingCase.booking_id == uuid.UUID(fixture["booking_id"])
            )
        )
        assert invoice is None
        assert blocked_case is not None
        assert blocked_case.payment_provider == "paypal"
        assert blocked_case.provider_account_id == "merchant_story_pp17"
        assert blocked_case.reason_code == "provider_error"
        assert blocked_case.provider_operation == "paypal_order_create"
        assert blocked_case.provider_http_status == 502
        assert blocked_case.provider_error_code == "INTERNAL_SERVER_ERROR"
        assert blocked_case.status == "open"


def test_capture_order_marks_invoice_paid_and_creates_payment_event():
    fixture = _insert_paypal_creator_with_booking(
        email=f"paypal_orders_capture_{uuid.uuid4().hex}@example.com"
    )
    issued_at = datetime.now(timezone.utc).replace(microsecond=0)
    with _engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO invoices "
                "(id, creator_id, booking_id, tid, payment_provider, provider_account_id, provider_invoice_id, provider_action_url, stripe_account_id, stripe_invoice_id, amount_cents, currency, status, issued_at, paid_at, voided_at) "
                "VALUES "
                "(:id, :creator_id, :booking_id, :tid, :payment_provider, :provider_account_id, :provider_invoice_id, :provider_action_url, :stripe_account_id, :stripe_invoice_id, :amount_cents, :currency, :status, :issued_at, :paid_at, :voided_at)"
            ),
            {
                "id": str(uuid.uuid4()),
                "creator_id": fixture["creator_id"],
                "booking_id": fixture["booking_id"],
                "tid": "paypal_order_tid",
                "payment_provider": "paypal",
                "provider_account_id": "merchant_story_pp17",
                "provider_invoice_id": "ORDER-story-pp17",
                "provider_action_url": "https://www.sandbox.paypal.com/checkoutnow?token=ORDER-story-pp17",
                "stripe_account_id": None,
                "stripe_invoice_id": None,
                "amount_cents": 15000,
                "currency": "USD",
                "status": "open",
                "issued_at": issued_at,
                "paid_at": None,
                "voided_at": None,
            },
        )

    provider = _StubPayPalOrdersProvider(
        captured_order=PayPalCheckoutCaptureResult(
            order_id="ORDER-story-pp17",
            status="COMPLETED",
            capture_id="CAPTURE-story-pp17",
            capture_status="COMPLETED",
            paid_at=issued_at + timedelta(minutes=5),
        )
    )
    service = PayPalOrdersService(
        session_factory=lambda: Session(_engine()),
        provider=provider,
    )

    result = service.capture_order(
        creator_id=uuid.UUID(fixture["creator_id"]),
        booking_id=uuid.UUID(fixture["booking_id"]),
        provider_order_id="ORDER-story-pp17",
    )

    assert result.outcome == "captured"
    assert result.capture_id == "CAPTURE-story-pp17"
    assert result.paid_at == issued_at + timedelta(minutes=5)
    assert provider.capture_calls == [
        {
            "provider_account_id": "merchant_story_pp17",
            "provider_order_id": "ORDER-story-pp17",
            "idempotency_key": "paypal:order:capture:ORDER-story-pp17",
        }
    ]

    with Session(_engine()) as session:
        invoice = session.scalar(
            select(Invoice).where(Invoice.booking_id == uuid.UUID(fixture["booking_id"]))
        )
        payment_event = session.scalar(
            select(InvoicePaymentEvent).where(
                InvoicePaymentEvent.payment_provider == "paypal",
                InvoicePaymentEvent.provider_event_id == "CAPTURE-story-pp17",
            )
        )
        assert invoice is not None
        assert invoice.status == "paid"
        assert invoice.paid_at == issued_at + timedelta(minutes=5)
        assert payment_event is not None
        assert payment_event.provider_event_type == "PAYMENT.CAPTURE.COMPLETED"
        assert payment_event.provider_invoice_id == "ORDER-story-pp17"
        assert payment_event.status == "applied"
