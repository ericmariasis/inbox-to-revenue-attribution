import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable, Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.models.billing_provider import (
    BILLING_CONNECT_STATUS_CONNECTED,
    BILLING_PROVIDER_PAYPAL,
)
from app.models.booking import Booking
from app.models.invoice import Invoice
from app.models.invoice_payment_event import InvoicePaymentEvent
from app.services.billing_lifecycle import resolve_billing_account_freeze
from app.services.billing_provider import BillingProviderError, get_billing_account_readiness
from app.services.billing_terms import resolve_booking_billing_terms
from app.services.blocked_billing import (
    BLOCKED_BILLING_REASON_CREATOR_NOT_BILLABLE,
    BLOCKED_BILLING_REASON_PROVIDER_ERROR,
    BLOCKED_BILLING_RESOLUTION_INVOICE_CREATED,
    BLOCKED_BILLING_RESOLUTION_INVOICE_EXISTING,
    record_blocked_billing_case,
    resolve_blocked_billing_case_for_invoice,
)
from app.services.booking_attribution import (
    BOOKING_ATTRIBUTION_STATUS_UNATTRIBUTED,
    get_booking_attribution_current_state,
)
from app.services.paypal_order_checkout import (
    build_paypal_order_checkout_cancel_url,
    build_paypal_order_checkout_return_url,
    build_paypal_order_checkout_state,
)
from app.services.paypal_provider import PayPalCheckoutCaptureResult, PayPalProvider


logger = logging.getLogger(__name__)

PayPalOrderStartOutcome = Literal["created", "existing"]
PayPalOrderCaptureOutcome = Literal["captured", "already_paid"]

PAYPAL_ORDER_FLOW_REASON_BOOKING_NOT_FOUND = "booking_not_found"
PAYPAL_ORDER_FLOW_REASON_BOOKING_NOT_ACTIVE = "booking_not_active"
PAYPAL_ORDER_FLOW_REASON_BOOKING_UNATTRIBUTED = "booking_unattributed"
PAYPAL_ORDER_FLOW_REASON_MISSING_BILLING_DEFAULTS = "missing_billing_defaults"
PAYPAL_ORDER_FLOW_REASON_BILLING_PROVIDER_NOT_PAYPAL = "billing_provider_not_paypal"
PAYPAL_ORDER_FLOW_REASON_CREATOR_NOT_BILLABLE = "creator_not_billable"
PAYPAL_ORDER_FLOW_REASON_EXISTING_NON_PAYPAL_INVOICE = "existing_non_paypal_invoice"
PAYPAL_ORDER_FLOW_REASON_INVOICE_ALREADY_PAID = "invoice_already_paid"
PAYPAL_ORDER_FLOW_REASON_INVOICE_NOT_OPEN = "invoice_not_open"
PAYPAL_ORDER_FLOW_REASON_INVOICE_MISSING_APPROVAL_URL = "invoice_missing_approval_url"
PAYPAL_ORDER_FLOW_REASON_INVALID_CALLBACK = "invalid_callback"
PAYPAL_ORDER_FLOW_REASON_PROVIDER_ERROR = "provider_error"


@dataclass(frozen=True)
class PayPalOrderStartResult:
    outcome: PayPalOrderStartOutcome
    invoice_id: uuid.UUID
    provider_order_id: str
    approval_url: str
    state: str


@dataclass(frozen=True)
class PayPalOrderCaptureOutcomeResult:
    outcome: PayPalOrderCaptureOutcome
    invoice_id: uuid.UUID
    provider_order_id: str
    capture_id: str | None
    paid_at: datetime | None


class PayPalOrderFlowError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        reason_code: str,
        status_code: int,
        provider_operation: str | None = None,
        provider_http_status: int | None = None,
        provider_error_code: str | None = None,
    ):
        super().__init__(message)
        self.reason_code = reason_code
        self.status_code = status_code
        self.provider_operation = provider_operation
        self.provider_http_status = provider_http_status
        self.provider_error_code = provider_error_code


class PayPalOrdersService:
    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        provider: PayPalProvider,
        settings: Settings | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ):
        self._session_factory = session_factory
        self._provider = provider
        self._settings = settings or get_settings()
        self._now_fn = now_fn or _utc_now

    def start_order(
        self,
        *,
        creator_id: uuid.UUID,
        booking_id: uuid.UUID,
    ) -> PayPalOrderStartResult:
        with self._session_factory() as session:
            booking = session.get(Booking, booking_id)
            if booking is None or booking.creator_id != creator_id:
                raise PayPalOrderFlowError(
                    "paypal order start not found",
                    reason_code=PAYPAL_ORDER_FLOW_REASON_BOOKING_NOT_FOUND,
                    status_code=404,
                )

            existing_invoice = session.scalar(
                select(Invoice).where(Invoice.booking_id == booking.id).with_for_update()
            )
            if existing_invoice is not None:
                return self._start_result_from_existing_invoice(
                    invoice=existing_invoice,
                    booking_id=booking.id,
                )

            if booking.status != "created":
                raise PayPalOrderFlowError(
                    "paypal order start requires an active booking",
                    reason_code=PAYPAL_ORDER_FLOW_REASON_BOOKING_NOT_ACTIVE,
                    status_code=409,
                )

            attribution = get_booking_attribution_current_state(booking=booking)
            if attribution.status == BOOKING_ATTRIBUTION_STATUS_UNATTRIBUTED:
                raise PayPalOrderFlowError(
                    "paypal order start requires attributed booking context",
                    reason_code=PAYPAL_ORDER_FLOW_REASON_BOOKING_UNATTRIBUTED,
                    status_code=409,
                )

            amount_cents, currency = resolve_booking_billing_terms(booking=booking)
            if amount_cents is None or currency is None:
                raise PayPalOrderFlowError(
                    "paypal order start requires billing defaults",
                    reason_code=PAYPAL_ORDER_FLOW_REASON_MISSING_BILLING_DEFAULTS,
                    status_code=409,
                )

            creator = booking.creator
            account_freeze = resolve_billing_account_freeze(booking=booking)
            if (
                creator.resolved_billing_connect_status != BILLING_CONNECT_STATUS_CONNECTED
                or creator.resolved_billing_provider != BILLING_PROVIDER_PAYPAL
                or account_freeze.payment_provider != BILLING_PROVIDER_PAYPAL
            ):
                raise PayPalOrderFlowError(
                    "paypal order start requires a connected PayPal billing account",
                    reason_code=PAYPAL_ORDER_FLOW_REASON_BILLING_PROVIDER_NOT_PAYPAL,
                    status_code=409,
                )

            provider_account_id = account_freeze.provider_account_id
            if provider_account_id is None:
                self._record_creator_not_billable(
                    session=session,
                    booking=booking,
                    amount_cents=amount_cents,
                    currency=currency,
                )
                session.commit()
                raise PayPalOrderFlowError(
                    "paypal order start requires a billable PayPal seller",
                    reason_code=PAYPAL_ORDER_FLOW_REASON_CREATOR_NOT_BILLABLE,
                    status_code=409,
                )

            try:
                readiness = get_billing_account_readiness(
                    provider=self._provider,
                    provider_account_id=provider_account_id,
                )
            except BillingProviderError as exc:
                self._record_provider_error(
                    session=session,
                    booking=booking,
                    amount_cents=amount_cents,
                    currency=currency,
                    provider_account_id=provider_account_id,
                    provider_operation=exc.operation,
                    provider_http_status=exc.http_status,
                    provider_error_code=exc.error_code,
                )
                session.commit()
                raise PayPalOrderFlowError(
                    "paypal order start unavailable",
                    reason_code=PAYPAL_ORDER_FLOW_REASON_PROVIDER_ERROR,
                    status_code=503,
                    provider_operation=exc.operation,
                    provider_http_status=exc.http_status,
                    provider_error_code=exc.error_code,
                ) from exc

            if not readiness.can_create_invoices:
                self._record_creator_not_billable(
                    session=session,
                    booking=booking,
                    amount_cents=amount_cents,
                    currency=currency,
                )
                session.commit()
                raise PayPalOrderFlowError(
                    "paypal order start requires a billable PayPal seller",
                    reason_code=PAYPAL_ORDER_FLOW_REASON_CREATOR_NOT_BILLABLE,
                    status_code=409,
                )

            state = build_paypal_order_checkout_state(
                creator_id=str(creator.id),
                booking_id=str(booking.id),
                settings=self._settings,
            )
            return_url = build_paypal_order_checkout_return_url(
                state=state,
                settings=self._settings,
            )
            cancel_url = build_paypal_order_checkout_cancel_url(
                state=state,
                settings=self._settings,
            )

            try:
                created_order = self._provider.create_checkout_order(
                    provider_account_id=provider_account_id,
                    amount_cents=amount_cents,
                    currency=currency.upper(),
                    return_url=return_url,
                    cancel_url=cancel_url,
                    idempotency_key=f"paypal:order:start:{booking.id}",
                    custom_id=str(booking.id),
                    payer_email=booking.email,
                )
            except BillingProviderError as exc:
                self._record_provider_error(
                    session=session,
                    booking=booking,
                    amount_cents=amount_cents,
                    currency=currency,
                    provider_account_id=provider_account_id,
                    provider_operation=exc.operation,
                    provider_http_status=exc.http_status,
                    provider_error_code=exc.error_code,
                )
                session.commit()
                raise PayPalOrderFlowError(
                    "paypal order start unavailable",
                    reason_code=PAYPAL_ORDER_FLOW_REASON_PROVIDER_ERROR,
                    status_code=503,
                    provider_operation=exc.operation,
                    provider_http_status=exc.http_status,
                    provider_error_code=exc.error_code,
                ) from exc

            issued_at = self._now_fn()
            invoice = Invoice(
                creator_id=creator.id,
                booking_id=booking.id,
                tid=booking.tid,
                payment_provider=BILLING_PROVIDER_PAYPAL,
                provider_account_id=provider_account_id,
                provider_invoice_id=created_order.order_id,
                provider_action_url=created_order.approval_url,
                amount_cents=amount_cents,
                currency=currency.upper(),
                status="open",
                issued_at=issued_at,
                paid_at=None,
            )
            session.add(invoice)

            try:
                session.flush()
            except IntegrityError:
                session.rollback()
                booking = session.get(Booking, booking_id)
                if booking is None:
                    raise PayPalOrderFlowError(
                        "paypal order start not found",
                        reason_code=PAYPAL_ORDER_FLOW_REASON_BOOKING_NOT_FOUND,
                        status_code=404,
                    )
                existing_invoice = session.scalar(
                    select(Invoice).where(Invoice.booking_id == booking.id)
                )
                if existing_invoice is None:
                    raise
                return self._start_result_from_existing_invoice(
                    invoice=existing_invoice,
                    booking_id=booking.id,
                )

            resolve_blocked_billing_case_for_invoice(
                session,
                booking_id=booking.id,
                invoice=invoice,
                resolved_at=issued_at,
                resolution_code=BLOCKED_BILLING_RESOLUTION_INVOICE_CREATED,
            )
            session.commit()
            session.refresh(invoice)

            logger.info(
                "paypal_order_start_created creator_id=%s booking_id=%s invoice_id=%s provider_order_id=%s",
                creator.id,
                booking.id,
                invoice.id,
                created_order.order_id,
            )
            return PayPalOrderStartResult(
                outcome="created",
                invoice_id=invoice.id,
                provider_order_id=created_order.order_id,
                approval_url=created_order.approval_url,
                state=state,
            )

    def capture_order(
        self,
        *,
        creator_id: uuid.UUID,
        booking_id: uuid.UUID,
        provider_order_id: str,
    ) -> PayPalOrderCaptureOutcomeResult:
        with self._session_factory() as session:
            booking = session.get(Booking, booking_id)
            if booking is None or booking.creator_id != creator_id:
                raise PayPalOrderFlowError(
                    "invalid paypal order callback",
                    reason_code=PAYPAL_ORDER_FLOW_REASON_INVALID_CALLBACK,
                    status_code=400,
                )

            invoice = session.scalar(
                select(Invoice).where(Invoice.booking_id == booking.id).with_for_update()
            )
            if (
                invoice is None
                or invoice.creator_id != creator_id
                or invoice.resolved_payment_provider != BILLING_PROVIDER_PAYPAL
                or invoice.resolved_provider_invoice_id != provider_order_id
            ):
                raise PayPalOrderFlowError(
                    "invalid paypal order callback",
                    reason_code=PAYPAL_ORDER_FLOW_REASON_INVALID_CALLBACK,
                    status_code=400,
                )

            if invoice.status == "paid":
                existing_event = session.scalar(
                    select(InvoicePaymentEvent)
                    .where(
                        InvoicePaymentEvent.invoice_id == invoice.id,
                        InvoicePaymentEvent.payment_provider == BILLING_PROVIDER_PAYPAL,
                    )
                    .order_by(InvoicePaymentEvent.received_at.desc())
                )
                return PayPalOrderCaptureOutcomeResult(
                    outcome="already_paid",
                    invoice_id=invoice.id,
                    provider_order_id=provider_order_id,
                    capture_id=(
                        existing_event.resolved_provider_event_id
                        if existing_event is not None
                        else None
                    ),
                    paid_at=invoice.paid_at,
                )

            if invoice.status != "open":
                raise PayPalOrderFlowError(
                    "invalid paypal order callback",
                    reason_code=PAYPAL_ORDER_FLOW_REASON_INVOICE_NOT_OPEN,
                    status_code=409,
                )

            provider_account_id = invoice.resolved_provider_account_id
            if provider_account_id is None:
                raise PayPalOrderFlowError(
                    "invalid paypal order callback",
                    reason_code=PAYPAL_ORDER_FLOW_REASON_INVALID_CALLBACK,
                    status_code=400,
                )

            try:
                capture = self._provider.capture_checkout_order(
                    provider_account_id=provider_account_id,
                    provider_order_id=provider_order_id,
                    idempotency_key=f"paypal:order:capture:{provider_order_id}",
                )
            except BillingProviderError as exc:
                raise PayPalOrderFlowError(
                    "paypal order capture unavailable",
                    reason_code=PAYPAL_ORDER_FLOW_REASON_PROVIDER_ERROR,
                    status_code=503,
                    provider_operation=exc.operation,
                    provider_http_status=exc.http_status,
                    provider_error_code=exc.error_code,
                ) from exc

            paid_at = capture.paid_at or self._now_fn()
            invoice.status = "paid"
            invoice.paid_at = paid_at

            existing_event = session.scalar(
                select(InvoicePaymentEvent).where(
                    InvoicePaymentEvent.payment_provider == BILLING_PROVIDER_PAYPAL,
                    InvoicePaymentEvent.provider_event_id == capture.capture_id,
                )
            )
            if existing_event is None:
                session.add(
                    InvoicePaymentEvent(
                        payment_provider=BILLING_PROVIDER_PAYPAL,
                        provider_event_id=capture.capture_id,
                        provider_event_type="PAYMENT.CAPTURE.COMPLETED",
                        provider_account_id=provider_account_id,
                        provider_invoice_id=provider_order_id,
                        invoice_id=invoice.id,
                        creator_id=booking.creator_id,
                        booking_id=booking.id,
                        tid=booking.tid,
                        status="applied",
                        paid_at=paid_at,
                        received_at=self._now_fn(),
                        processed_at=self._now_fn(),
                    )
                )

            resolve_blocked_billing_case_for_invoice(
                session,
                booking_id=booking.id,
                invoice=invoice,
                resolved_at=paid_at,
                resolution_code=BLOCKED_BILLING_RESOLUTION_INVOICE_EXISTING,
            )
            session.commit()

            logger.info(
                "paypal_order_capture_completed creator_id=%s booking_id=%s invoice_id=%s provider_order_id=%s capture_id=%s",
                creator_id,
                booking.id,
                invoice.id,
                provider_order_id,
                capture.capture_id,
            )
            return PayPalOrderCaptureOutcomeResult(
                outcome="captured",
                invoice_id=invoice.id,
                provider_order_id=provider_order_id,
                capture_id=capture.capture_id,
                paid_at=paid_at,
            )

    def _start_result_from_existing_invoice(
        self,
        *,
        invoice: Invoice,
        booking_id: uuid.UUID,
    ) -> PayPalOrderStartResult:
        if invoice.resolved_payment_provider != BILLING_PROVIDER_PAYPAL:
            raise PayPalOrderFlowError(
                "paypal order start found a non-PayPal invoice for this booking",
                reason_code=PAYPAL_ORDER_FLOW_REASON_EXISTING_NON_PAYPAL_INVOICE,
                status_code=409,
            )
        if invoice.status == "paid":
            raise PayPalOrderFlowError(
                "paypal order start found a paid invoice for this booking",
                reason_code=PAYPAL_ORDER_FLOW_REASON_INVOICE_ALREADY_PAID,
                status_code=409,
            )
        if invoice.status != "open":
            raise PayPalOrderFlowError(
                "paypal order start found a non-open invoice for this booking",
                reason_code=PAYPAL_ORDER_FLOW_REASON_INVOICE_NOT_OPEN,
                status_code=409,
            )
        if invoice.provider_action_url is None or invoice.resolved_provider_invoice_id is None:
            raise PayPalOrderFlowError(
                "paypal order start found an incomplete PayPal invoice record",
                reason_code=PAYPAL_ORDER_FLOW_REASON_INVOICE_MISSING_APPROVAL_URL,
                status_code=409,
            )
        state = build_paypal_order_checkout_state(
            creator_id=str(invoice.creator_id),
            booking_id=str(booking_id),
            settings=self._settings,
        )
        return PayPalOrderStartResult(
            outcome="existing",
            invoice_id=invoice.id,
            provider_order_id=invoice.resolved_provider_invoice_id,
            approval_url=invoice.provider_action_url,
            state=state,
        )

    def _record_creator_not_billable(
        self,
        *,
        session: Session,
        booking: Booking,
        amount_cents: int,
        currency: str,
    ) -> None:
        account_freeze = resolve_billing_account_freeze(booking=booking)
        record_blocked_billing_case(
            session,
            booking=booking,
            payment_provider=BILLING_PROVIDER_PAYPAL,
            frozen_amount_cents=amount_cents,
            frozen_currency=currency,
            reason_code=BLOCKED_BILLING_REASON_CREATOR_NOT_BILLABLE,
            blocked_at=self._now_fn(),
            provider_account_id=account_freeze.provider_account_id,
        )

    def _record_provider_error(
        self,
        *,
        session: Session,
        booking: Booking,
        amount_cents: int,
        currency: str,
        provider_account_id: str | None,
        provider_operation: str | None,
        provider_http_status: int | None,
        provider_error_code: str | None,
    ) -> None:
        record_blocked_billing_case(
            session,
            booking=booking,
            payment_provider=BILLING_PROVIDER_PAYPAL,
            frozen_amount_cents=amount_cents,
            frozen_currency=currency,
            reason_code=BLOCKED_BILLING_REASON_PROVIDER_ERROR,
            blocked_at=self._now_fn(),
            provider_account_id=provider_account_id,
            provider_operation=provider_operation,
            provider_http_status=provider_http_status,
            provider_error_code=provider_error_code,
        )


def _utc_now() -> datetime:
    return datetime.now(UTC)
