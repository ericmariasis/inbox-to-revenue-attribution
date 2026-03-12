import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable, Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.booking import Booking
from app.models.invoice import Invoice
from app.services.booking_attribution import (
    BOOKING_ATTRIBUTION_STATUS_UNATTRIBUTED,
    get_booking_attribution_current_state,
)
from app.services.blocked_billing import (
    BLOCKED_BILLING_REASON_CREATOR_NOT_BILLABLE,
    BLOCKED_BILLING_REASON_PROVIDER_ERROR,
    BLOCKED_BILLING_RESOLUTION_INVOICE_CREATED,
    BLOCKED_BILLING_RESOLUTION_INVOICE_EXISTING,
    record_blocked_billing_case,
    resolve_blocked_billing_case_for_invoice,
)
from app.services.stripe_account_readiness import creator_has_billable_stripe_account
from app.services.stripe_provider import StripeProvider, StripeProviderError


logger = logging.getLogger(__name__)


CreateInvoiceOutcome = Literal["created", "existing", "deferred"]
CreateInvoiceReason = Literal[
    "missing_billing_defaults",
    "creator_not_billable",
    "booking_not_found",
    "booking_not_active",
    "booking_unattributed",
    "provider_error",
]
VoidInvoiceOutcome = Literal["voided", "noop"]
VoidInvoiceReason = Literal["invoice_missing", "invoice_not_open", "invoice_already_void", "provider_error"]


@dataclass(frozen=True)
class BillingInvoiceResult:
    outcome: CreateInvoiceOutcome
    reason: CreateInvoiceReason | None = None
    invoice_id: uuid.UUID | None = None
    stripe_invoice_id: str | None = None
    invoice_status: str | None = None


@dataclass(frozen=True)
class BillingInvoiceVoidResult:
    outcome: VoidInvoiceOutcome
    reason: VoidInvoiceReason | None = None
    invoice_id: uuid.UUID | None = None
    stripe_invoice_id: str | None = None
    invoice_status: str | None = None


class BillingOrchestrator:
    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        provider: StripeProvider,
        now_fn: Callable[[], datetime] | None = None,
    ):
        self._session_factory = session_factory
        self._provider = provider
        self._now_fn = now_fn or _utc_now

    def create_invoice_for_booking(
        self,
        *,
        booking_id: uuid.UUID,
    ) -> BillingInvoiceResult:
        with self._session_factory() as session:
            booking = session.get(Booking, booking_id)
            if booking is None:
                logger.warning("billing_invoice_create_booking_missing booking_id=%s", booking_id)
                return BillingInvoiceResult(outcome="deferred", reason="booking_not_found")

            existing_invoice = session.scalar(select(Invoice).where(Invoice.booking_id == booking.id))
            if existing_invoice is not None:
                if resolve_blocked_billing_case_for_invoice(
                    session,
                    booking_id=booking.id,
                    invoice=existing_invoice,
                    resolved_at=self._now_fn(),
                    resolution_code=BLOCKED_BILLING_RESOLUTION_INVOICE_EXISTING,
                ):
                    session.commit()
                logger.info(
                    "billing_invoice_create_existing booking_id=%s invoice_id=%s stripe_invoice_id=%s status=%s",
                    booking.id,
                    existing_invoice.id,
                    existing_invoice.stripe_invoice_id,
                    existing_invoice.status,
                )
                return _billing_invoice_result(existing_invoice, outcome="existing")

            attribution = get_booking_attribution_current_state(booking=booking)
            if attribution.status == BOOKING_ATTRIBUTION_STATUS_UNATTRIBUTED:
                logger.info(
                    "billing_invoice_create_deferred_booking_unattributed booking_id=%s creator_id=%s booking_link_id=%s unattributed_reason=%s",
                    booking.id,
                    booking.creator_id,
                    booking.booking_link_id,
                    attribution.unattributed_reason,
                )
                return BillingInvoiceResult(
                    outcome="deferred",
                    reason="booking_unattributed",
                )

            amount_cents, currency = _resolve_booking_billing_terms(booking=booking)
            if amount_cents is None or currency is None:
                logger.info(
                    "billing_invoice_create_deferred_missing_billing_defaults booking_id=%s creator_id=%s booking_link_id=%s missing_amount=%s missing_currency=%s",
                    booking.id,
                    booking.creator_id,
                    booking.booking_link_id,
                    amount_cents is None,
                    currency is None,
                )
                return BillingInvoiceResult(
                    outcome="deferred",
                    reason="missing_billing_defaults",
                )

            if booking.status != "created":
                logger.info(
                    "billing_invoice_create_deferred_booking_not_active booking_id=%s creator_id=%s status=%s",
                    booking.id,
                    booking.creator_id,
                    booking.status,
                )
                return BillingInvoiceResult(outcome="deferred", reason="booking_not_active")

            creator = booking.creator
            try:
                creator_is_billable = creator_has_billable_stripe_account(
                    creator=creator,
                    provider=self._provider,
                )
            except StripeProviderError as exc:
                record_blocked_billing_case(
                    session,
                    booking=booking,
                    frozen_amount_cents=amount_cents,
                    frozen_currency=currency,
                    reason_code=BLOCKED_BILLING_REASON_PROVIDER_ERROR,
                    blocked_at=self._now_fn(),
                    stripe_account_id=creator.stripe_account_id,
                    provider_operation=exc.operation,
                    provider_http_status=exc.http_status,
                    provider_error_code=exc.error_code,
                )
                session.commit()
                logger.warning(
                    "billing_invoice_create_deferred_provider_error booking_id=%s creator_id=%s stripe_account_id=%s operation=%s http_status=%s error_code=%s",
                    booking.id,
                    creator.id,
                    creator.stripe_account_id,
                    exc.operation,
                    exc.http_status,
                    exc.error_code,
                )
                return BillingInvoiceResult(
                    outcome="deferred",
                    reason="provider_error",
                )

            if not creator_is_billable:
                record_blocked_billing_case(
                    session,
                    booking=booking,
                    frozen_amount_cents=amount_cents,
                    frozen_currency=currency,
                    reason_code=BLOCKED_BILLING_REASON_CREATOR_NOT_BILLABLE,
                    blocked_at=self._now_fn(),
                    stripe_account_id=creator.stripe_account_id,
                )
                session.commit()
                logger.info(
                    "billing_invoice_create_deferred_creator_not_billable booking_id=%s creator_id=%s stripe_account_id=%s",
                    booking.id,
                    creator.id,
                    creator.stripe_account_id,
                )
                return BillingInvoiceResult(
                    outcome="deferred",
                    reason="creator_not_billable",
                )

            stripe_account_id = creator.stripe_account_id
            if stripe_account_id is None:
                record_blocked_billing_case(
                    session,
                    booking=booking,
                    frozen_amount_cents=amount_cents,
                    frozen_currency=currency,
                    reason_code=BLOCKED_BILLING_REASON_CREATOR_NOT_BILLABLE,
                    blocked_at=self._now_fn(),
                    stripe_account_id=stripe_account_id,
                )
                session.commit()
                logger.info(
                    "billing_invoice_create_deferred_creator_not_billable booking_id=%s creator_id=%s stripe_account_id=%s",
                    booking.id,
                    creator.id,
                    stripe_account_id,
                )
                return BillingInvoiceResult(
                    outcome="deferred",
                    reason="creator_not_billable",
                )

            try:
                created_invoice = self._provider.create_invoice(
                    stripe_account_id=stripe_account_id,
                    amount_cents=amount_cents,
                    currency=currency.upper(),
                    metadata={
                        "creator_id": str(creator.id),
                        "booking_uuid": booking.calendly_booking_uuid,
                        "tid": booking.tid,
                    },
                    idempotency_key=f"billing:create:{booking.calendly_booking_uuid}",
                )
            except StripeProviderError as exc:
                record_blocked_billing_case(
                    session,
                    booking=booking,
                    frozen_amount_cents=amount_cents,
                    frozen_currency=currency,
                    reason_code=BLOCKED_BILLING_REASON_PROVIDER_ERROR,
                    blocked_at=self._now_fn(),
                    stripe_account_id=stripe_account_id,
                    provider_operation=exc.operation,
                    provider_http_status=exc.http_status,
                    provider_error_code=exc.error_code,
                )
                session.commit()
                logger.warning(
                    "billing_invoice_create_deferred_provider_error booking_id=%s creator_id=%s stripe_account_id=%s operation=%s http_status=%s error_code=%s",
                    booking.id,
                    creator.id,
                    stripe_account_id,
                    exc.operation,
                    exc.http_status,
                    exc.error_code,
                )
                return BillingInvoiceResult(
                    outcome="deferred",
                    reason="provider_error",
                )

            issued_at = self._now_fn()
            invoice = Invoice(
                creator_id=creator.id,
                booking_id=booking.id,
                tid=booking.tid,
                stripe_account_id=stripe_account_id,
                stripe_invoice_id=created_invoice.stripe_invoice_id,
                amount_cents=amount_cents,
                currency=currency.upper(),
                status=created_invoice.status,
                issued_at=issued_at,
                paid_at=issued_at if created_invoice.status == "paid" else None,
            )
            session.add(invoice)

            try:
                session.flush()
            except IntegrityError:
                session.rollback()
                existing_invoice = session.scalar(select(Invoice).where(Invoice.booking_id == booking.id))
                if existing_invoice is None:
                    raise
                if resolve_blocked_billing_case_for_invoice(
                    session,
                    booking_id=booking.id,
                    invoice=existing_invoice,
                    resolved_at=self._now_fn(),
                    resolution_code=BLOCKED_BILLING_RESOLUTION_INVOICE_EXISTING,
                ):
                    session.commit()
                logger.info(
                    "billing_invoice_create_existing booking_id=%s invoice_id=%s stripe_invoice_id=%s status=%s",
                    booking.id,
                    existing_invoice.id,
                    existing_invoice.stripe_invoice_id,
                    existing_invoice.status,
                )
                return _billing_invoice_result(existing_invoice, outcome="existing")

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
                "billing_invoice_created booking_id=%s invoice_id=%s stripe_invoice_id=%s creator_id=%s tid=%s amount_cents=%s currency=%s",
                booking.id,
                invoice.id,
                invoice.stripe_invoice_id,
                invoice.creator_id,
                invoice.tid,
                invoice.amount_cents,
                invoice.currency,
            )
            return _billing_invoice_result(invoice, outcome="created")

    def void_open_invoice_for_booking(self, *, booking_id: uuid.UUID) -> BillingInvoiceVoidResult:
        with self._session_factory() as session:
            invoice = session.scalar(select(Invoice).where(Invoice.booking_id == booking_id))
            if invoice is None:
                logger.info("billing_invoice_void_noop_missing_invoice booking_id=%s", booking_id)
                return BillingInvoiceVoidResult(outcome="noop", reason="invoice_missing")

            if invoice.status == "void":
                logger.info(
                    "billing_invoice_void_noop_already_void booking_id=%s invoice_id=%s stripe_invoice_id=%s",
                    booking_id,
                    invoice.id,
                    invoice.stripe_invoice_id,
                )
                return _billing_invoice_void_result(invoice, outcome="noop", reason="invoice_already_void")

            if invoice.status != "open":
                logger.info(
                    "billing_invoice_void_noop_not_open booking_id=%s invoice_id=%s stripe_invoice_id=%s status=%s",
                    booking_id,
                    invoice.id,
                    invoice.stripe_invoice_id,
                    invoice.status,
                )
                return _billing_invoice_void_result(invoice, outcome="noop", reason="invoice_not_open")

            try:
                self._provider.void_invoice(
                    stripe_account_id=invoice.stripe_account_id,
                    stripe_invoice_id=invoice.stripe_invoice_id,
                )
            except StripeProviderError as exc:
                logger.warning(
                    "billing_invoice_void_noop_provider_error booking_id=%s invoice_id=%s stripe_invoice_id=%s stripe_account_id=%s operation=%s http_status=%s error_code=%s",
                    booking_id,
                    invoice.id,
                    invoice.stripe_invoice_id,
                    invoice.stripe_account_id,
                    exc.operation,
                    exc.http_status,
                    exc.error_code,
                )
                return _billing_invoice_void_result(
                    invoice,
                    outcome="noop",
                    reason="provider_error",
                )
            invoice.status = "void"
            invoice.voided_at = self._now_fn()
            session.commit()
            session.refresh(invoice)

            logger.info(
                "billing_invoice_voided booking_id=%s invoice_id=%s stripe_invoice_id=%s voided_at=%s",
                booking_id,
                invoice.id,
                invoice.stripe_invoice_id,
                invoice.voided_at.isoformat(),
            )
            return _billing_invoice_void_result(invoice, outcome="voided")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _billing_invoice_result(
    invoice: Invoice,
    *,
    outcome: CreateInvoiceOutcome,
) -> BillingInvoiceResult:
    return BillingInvoiceResult(
        outcome=outcome,
        invoice_id=invoice.id,
        stripe_invoice_id=invoice.stripe_invoice_id,
        invoice_status=invoice.status,
    )


def _billing_invoice_void_result(
    invoice: Invoice,
    *,
    outcome: VoidInvoiceOutcome,
    reason: VoidInvoiceReason | None = None,
) -> BillingInvoiceVoidResult:
    return BillingInvoiceVoidResult(
        outcome=outcome,
        reason=reason,
        invoice_id=invoice.id,
        stripe_invoice_id=invoice.stripe_invoice_id,
        invoice_status=invoice.status,
    )


def _resolve_booking_billing_terms(*, booking: Booking) -> tuple[int | None, str | None]:
    frozen_amount_cents = booking.frozen_billing_amount_cents
    frozen_currency = booking.frozen_billing_currency
    if frozen_amount_cents is not None and frozen_currency is not None:
        return frozen_amount_cents, frozen_currency.upper()

    blocked_case = booking.blocked_billing_case
    if blocked_case is not None:
        booking.frozen_billing_amount_cents = blocked_case.frozen_amount_cents
        booking.frozen_billing_currency = blocked_case.frozen_currency.upper()
        return booking.frozen_billing_amount_cents, booking.frozen_billing_currency

    if frozen_amount_cents is not None or frozen_currency is not None:
        logger.warning(
            "billing_booking_frozen_billing_partial booking_id=%s creator_id=%s amount_present=%s currency_present=%s",
            booking.id,
            booking.creator_id,
            frozen_amount_cents is not None,
            frozen_currency is not None,
        )

    billing_amount_cents = booking.booking_link.billing_amount_cents
    billing_currency = booking.booking_link.billing_currency
    if billing_amount_cents is None or billing_currency is None:
        return billing_amount_cents, billing_currency.upper() if billing_currency else None

    booking.frozen_billing_amount_cents = billing_amount_cents
    booking.frozen_billing_currency = billing_currency.upper()
    return booking.frozen_billing_amount_cents, booking.frozen_billing_currency
