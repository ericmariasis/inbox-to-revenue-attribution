import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable, Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.booking import Booking
from app.models.booking_provider import BOOKING_PROVIDER_CALENDLY
from app.models.billing_provider import BILLING_PROVIDER_STRIPE
from app.models.invoice import Invoice
from app.services.billing_lifecycle import resolve_billing_account_freeze
from app.services.billing_terms import resolve_booking_billing_terms
from app.services.billing_provider import (
    BillingProvider,
    BillingProviderError,
    BillingProviderRegistry,
    BillingProviderResolutionError,
    create_billing_invoice,
    get_billing_account_readiness,
    resolve_billing_provider,
    stop_billing_invoice,
)
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


logger = logging.getLogger(__name__)


CreateInvoiceOutcome = Literal["created", "existing", "deferred"]
CreateInvoiceReason = Literal[
    "missing_billing_defaults",
    "creator_not_billable",
    "booking_not_found",
    "missing_provider_booking_identity",
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
    provider_account_id: str | None = None
    provider_invoice_id: str | None = None
    invoice_status: str | None = None

    @property
    def stripe_invoice_id(self) -> str | None:
        return self.provider_invoice_id


@dataclass(frozen=True)
class BillingInvoiceVoidResult:
    outcome: VoidInvoiceOutcome
    reason: VoidInvoiceReason | None = None
    invoice_id: uuid.UUID | None = None
    provider_account_id: str | None = None
    provider_invoice_id: str | None = None
    invoice_status: str | None = None

    @property
    def stripe_invoice_id(self) -> str | None:
        return self.provider_invoice_id


class BillingOrchestrator:
    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        provider: BillingProvider | None = None,
        providers: BillingProviderRegistry | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ):
        if provider is None and providers is None:
            raise TypeError("provider or providers is required")
        self._session_factory = session_factory
        self._provider = provider
        self._providers = providers
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
                    "billing_invoice_create_existing booking_id=%s invoice_id=%s provider_invoice_id=%s status=%s",
                    booking.id,
                    existing_invoice.id,
                    existing_invoice.resolved_provider_invoice_id,
                    existing_invoice.status,
                )
                return _billing_invoice_result(existing_invoice, outcome="existing")

            booking_provider, provider_booking_id = _resolve_booking_provider_identity(booking=booking)
            if provider_booking_id is None:
                logger.warning(
                    "billing_invoice_create_deferred_missing_provider_booking_identity booking_id=%s creator_id=%s provider=%s",
                    booking.id,
                    booking.creator_id,
                    booking_provider,
                )
                return BillingInvoiceResult(
                    outcome="deferred",
                    reason="missing_provider_booking_identity",
                )

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

            amount_cents, currency = resolve_booking_billing_terms(booking=booking)
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
            account_freeze = resolve_billing_account_freeze(booking=booking)
            provider_name = account_freeze.payment_provider
            try:
                provider = self._provider_for_name(provider_name=provider_name)
            except BillingProviderResolutionError:
                logger.warning(
                    "billing_invoice_create_deferred_provider_missing booking_id=%s creator_id=%s billing_provider=%s",
                    booking.id,
                    creator.id,
                    provider_name,
                )
                return BillingInvoiceResult(
                    outcome="deferred",
                    reason="provider_error",
                )

            provider_account_id = account_freeze.provider_account_id
            try:
                readiness = (
                    get_billing_account_readiness(
                        provider=provider,
                        provider_account_id=provider_account_id,
                    )
                    if provider_account_id is not None
                    else None
                )
                creator_is_billable = readiness is not None and readiness.can_create_invoices
            except BillingProviderError as exc:
                record_blocked_billing_case(
                    session,
                    booking=booking,
                    payment_provider=provider_name,
                    frozen_amount_cents=amount_cents,
                    frozen_currency=currency,
                    reason_code=BLOCKED_BILLING_REASON_PROVIDER_ERROR,
                    blocked_at=self._now_fn(),
                    provider_account_id=provider_account_id,
                    provider_operation=exc.operation,
                    provider_http_status=exc.http_status,
                    provider_error_code=exc.error_code,
                )
                session.commit()
                logger.warning(
                    "billing_invoice_create_deferred_provider_error booking_id=%s creator_id=%s provider_account_id=%s operation=%s http_status=%s error_code=%s",
                    booking.id,
                    creator.id,
                    provider_account_id,
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
                    payment_provider=provider_name,
                    frozen_amount_cents=amount_cents,
                    frozen_currency=currency,
                    reason_code=BLOCKED_BILLING_REASON_CREATOR_NOT_BILLABLE,
                    blocked_at=self._now_fn(),
                    provider_account_id=provider_account_id,
                )
                session.commit()
                logger.info(
                    "billing_invoice_create_deferred_creator_not_billable booking_id=%s creator_id=%s provider_account_id=%s",
                    booking.id,
                    creator.id,
                    provider_account_id,
                )
                return BillingInvoiceResult(
                    outcome="deferred",
                    reason="creator_not_billable",
                )

            if provider_account_id is None:
                record_blocked_billing_case(
                    session,
                    booking=booking,
                    payment_provider=provider_name,
                    frozen_amount_cents=amount_cents,
                    frozen_currency=currency,
                    reason_code=BLOCKED_BILLING_REASON_CREATOR_NOT_BILLABLE,
                    blocked_at=self._now_fn(),
                    provider_account_id=provider_account_id,
                )
                session.commit()
                logger.info(
                    "billing_invoice_create_deferred_creator_not_billable booking_id=%s creator_id=%s provider_account_id=%s",
                    booking.id,
                    creator.id,
                    provider_account_id,
                )
                return BillingInvoiceResult(
                    outcome="deferred",
                    reason="creator_not_billable",
                )

            try:
                created_invoice = create_billing_invoice(
                    provider=provider,
                    provider_account_id=provider_account_id,
                    amount_cents=amount_cents,
                    currency=currency.upper(),
                    metadata=_billing_provider_metadata(
                        creator_id=creator.id,
                        booking_provider=booking_provider,
                        provider_booking_id=provider_booking_id,
                        tid=booking.tid,
                    ),
                    idempotency_key=f"billing:create:{booking_provider}:{provider_booking_id}",
                )
                _validate_created_invoice_status(
                    provider_name=provider_name,
                    invoice_status=created_invoice.invoice_status,
                )
            except BillingProviderError as exc:
                record_blocked_billing_case(
                    session,
                    booking=booking,
                    payment_provider=provider_name,
                    frozen_amount_cents=amount_cents,
                    frozen_currency=currency,
                    reason_code=BLOCKED_BILLING_REASON_PROVIDER_ERROR,
                    blocked_at=self._now_fn(),
                    provider_account_id=provider_account_id,
                    provider_operation=exc.operation,
                    provider_http_status=exc.http_status,
                    provider_error_code=exc.error_code,
                )
                session.commit()
                logger.warning(
                    "billing_invoice_create_deferred_provider_error booking_id=%s creator_id=%s provider_account_id=%s operation=%s http_status=%s error_code=%s",
                    booking.id,
                    creator.id,
                    provider_account_id,
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
                payment_provider=provider_name,
                provider_account_id=created_invoice.provider_account_id,
                provider_invoice_id=created_invoice.provider_invoice_id,
                stripe_account_id=_legacy_stripe_account_id(
                    payment_provider=provider_name,
                    provider_account_id=created_invoice.provider_account_id,
                ),
                stripe_invoice_id=_legacy_stripe_invoice_id(
                    payment_provider=provider_name,
                    provider_invoice_id=created_invoice.provider_invoice_id,
                ),
                amount_cents=amount_cents,
                currency=currency.upper(),
                status=created_invoice.invoice_status,
                issued_at=issued_at,
                paid_at=issued_at if created_invoice.invoice_status == "paid" else None,
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
                    "billing_invoice_create_existing booking_id=%s invoice_id=%s provider_invoice_id=%s status=%s",
                    booking.id,
                    existing_invoice.id,
                    existing_invoice.resolved_provider_invoice_id,
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
                "billing_invoice_created booking_id=%s provider=%s provider_booking_id=%s invoice_id=%s provider_invoice_id=%s creator_id=%s tid=%s amount_cents=%s currency=%s",
                booking.id,
                booking_provider,
                provider_booking_id,
                invoice.id,
                invoice.resolved_provider_invoice_id,
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
                    "billing_invoice_void_noop_already_void booking_id=%s invoice_id=%s provider_invoice_id=%s",
                    booking_id,
                    invoice.id,
                    invoice.resolved_provider_invoice_id,
                )
                return _billing_invoice_void_result(invoice, outcome="noop", reason="invoice_already_void")

            if invoice.status != "open":
                logger.info(
                    "billing_invoice_void_noop_not_open booking_id=%s invoice_id=%s provider_invoice_id=%s status=%s",
                    booking_id,
                    invoice.id,
                    invoice.resolved_provider_invoice_id,
                    invoice.status,
                )
                return _billing_invoice_void_result(invoice, outcome="noop", reason="invoice_not_open")

            provider_account_id = invoice.resolved_provider_account_id
            provider_invoice_id = invoice.resolved_provider_invoice_id
            if provider_account_id is None or provider_invoice_id is None:
                logger.warning(
                    "billing_invoice_void_noop_missing_provider_identity booking_id=%s invoice_id=%s payment_provider=%s provider_account_id=%s provider_invoice_id=%s",
                    booking_id,
                    invoice.id,
                    invoice.resolved_payment_provider,
                    provider_account_id,
                    provider_invoice_id,
                )
                return _billing_invoice_void_result(
                    invoice,
                    outcome="noop",
                    reason="provider_error",
                )

            try:
                provider = self._provider_for_invoice(invoice=invoice)
            except BillingProviderResolutionError:
                logger.warning(
                    "billing_invoice_void_noop_provider_missing booking_id=%s invoice_id=%s payment_provider=%s",
                    booking_id,
                    invoice.id,
                    invoice.resolved_payment_provider,
                )
                return _billing_invoice_void_result(
                    invoice,
                    outcome="noop",
                    reason="provider_error",
                )

            try:
                stopped_invoice = stop_billing_invoice(
                    provider=provider,
                    provider_account_id=provider_account_id,
                    provider_invoice_id=provider_invoice_id,
                )
                _validate_stopped_invoice_status(
                    provider_name=invoice.resolved_payment_provider,
                    invoice_status=stopped_invoice.invoice_status,
                )
            except BillingProviderError as exc:
                logger.warning(
                    "billing_invoice_void_noop_provider_error booking_id=%s invoice_id=%s provider_invoice_id=%s provider_account_id=%s operation=%s http_status=%s error_code=%s",
                    booking_id,
                    invoice.id,
                    provider_invoice_id,
                    provider_account_id,
                    exc.operation,
                    exc.http_status,
                    exc.error_code,
                )
                return _billing_invoice_void_result(
                    invoice,
                    outcome="noop",
                    reason="provider_error",
                )
            invoice.status = stopped_invoice.invoice_status
            invoice.voided_at = self._now_fn()
            session.commit()
            session.refresh(invoice)

            logger.info(
                "billing_invoice_voided booking_id=%s invoice_id=%s provider_invoice_id=%s voided_at=%s",
                booking_id,
                invoice.id,
                invoice.resolved_provider_invoice_id,
                invoice.voided_at.isoformat(),
            )
            return _billing_invoice_void_result(invoice, outcome="voided")

    def _provider_for_name(self, *, provider_name: str | None) -> BillingProvider:
        if self._providers is not None:
            return resolve_billing_provider(
                providers=self._providers,
                provider_name=provider_name,
            )
        assert self._provider is not None
        return self._provider

    def _provider_for_invoice(self, *, invoice: Invoice) -> BillingProvider:
        if self._providers is not None:
            return resolve_billing_provider(
                providers=self._providers,
                provider_name=invoice.resolved_payment_provider,
            )
        assert self._provider is not None
        return self._provider


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
        provider_account_id=invoice.resolved_provider_account_id,
        provider_invoice_id=invoice.resolved_provider_invoice_id,
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
        provider_account_id=invoice.resolved_provider_account_id,
        provider_invoice_id=invoice.resolved_provider_invoice_id,
        invoice_status=invoice.status,
    )


def _resolve_booking_provider_identity(*, booking: Booking) -> tuple[str, str | None]:
    return booking.provider or BOOKING_PROVIDER_CALENDLY, booking.resolved_provider_booking_id


def _legacy_stripe_account_id(
    *,
    payment_provider: str,
    provider_account_id: str | None,
) -> str | None:
    if payment_provider == BILLING_PROVIDER_STRIPE:
        return provider_account_id
    return None


def _legacy_stripe_invoice_id(
    *,
    payment_provider: str,
    provider_invoice_id: str | None,
) -> str | None:
    if payment_provider == BILLING_PROVIDER_STRIPE:
        return provider_invoice_id
    return None


def _billing_provider_metadata(
    *,
    creator_id: uuid.UUID,
    booking_provider: str,
    provider_booking_id: str,
    tid: str | None,
) -> dict[str, str]:
    metadata = {
        "creator_id": str(creator_id),
        "booking_provider": booking_provider,
        "provider_booking_id": provider_booking_id,
        # Preserve the legacy Stripe metadata key until the later payment/reporting seam is widened.
        "booking_uuid": provider_booking_id,
    }
    if tid is not None:
        metadata["tid"] = tid
    return metadata


def _validate_created_invoice_status(*, provider_name: str, invoice_status: str) -> None:
    if invoice_status in {"open", "paid"}:
        return
    raise BillingProviderError(
        "billing provider returned unexpected invoice create status",
        provider_name=provider_name,
        operation="billing_invoice_create_status_validation",
        error_code=f"invoice_status_{invoice_status}",
    )


def _validate_stopped_invoice_status(*, provider_name: str, invoice_status: str) -> None:
    if invoice_status == "void":
        return
    raise BillingProviderError(
        "billing provider returned unexpected invoice stop status",
        provider_name=provider_name,
        operation="billing_invoice_stop_status_validation",
        error_code=f"invoice_status_{invoice_status}",
    )
