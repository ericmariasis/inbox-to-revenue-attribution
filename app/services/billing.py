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
from app.services.stripe_account_readiness import creator_has_billable_stripe_account
from app.services.stripe_provider import StripeProvider


logger = logging.getLogger(__name__)


CreateInvoiceOutcome = Literal["created", "existing", "deferred"]
CreateInvoiceReason = Literal["missing_billing_defaults", "creator_not_billable", "booking_not_found"]
VoidInvoiceOutcome = Literal["voided", "noop"]
VoidInvoiceReason = Literal["invoice_missing", "invoice_not_open", "invoice_already_void"]


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

    def create_invoice_for_booking(self, *, booking_id: uuid.UUID) -> BillingInvoiceResult:
        with self._session_factory() as session:
            booking = session.get(Booking, booking_id)
            if booking is None:
                logger.warning("billing_invoice_create_booking_missing booking_id=%s", booking_id)
                return BillingInvoiceResult(outcome="deferred", reason="booking_not_found")

            existing_invoice = session.scalar(select(Invoice).where(Invoice.booking_id == booking.id))
            if existing_invoice is not None:
                logger.info(
                    "billing_invoice_create_existing booking_id=%s invoice_id=%s stripe_invoice_id=%s status=%s",
                    booking.id,
                    existing_invoice.id,
                    existing_invoice.stripe_invoice_id,
                    existing_invoice.status,
                )
                return _billing_invoice_result(existing_invoice, outcome="existing")

            booking_link = booking.booking_link
            amount_cents = booking_link.billing_amount_cents
            currency = booking_link.billing_currency
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

            creator = booking.creator
            if not creator_has_billable_stripe_account(creator=creator, provider=self._provider):
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

            invoice = Invoice(
                creator_id=creator.id,
                booking_id=booking.id,
                tid=booking.tid,
                stripe_account_id=stripe_account_id,
                stripe_invoice_id=created_invoice.stripe_invoice_id,
                amount_cents=amount_cents,
                currency=currency.upper(),
                status="open",
                issued_at=self._now_fn(),
            )
            session.add(invoice)

            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                existing_invoice = session.scalar(select(Invoice).where(Invoice.booking_id == booking.id))
                if existing_invoice is None:
                    raise
                logger.info(
                    "billing_invoice_create_existing booking_id=%s invoice_id=%s stripe_invoice_id=%s status=%s",
                    booking.id,
                    existing_invoice.id,
                    existing_invoice.stripe_invoice_id,
                    existing_invoice.status,
                )
                return _billing_invoice_result(existing_invoice, outcome="existing")

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

            self._provider.void_invoice(
                stripe_account_id=invoice.stripe_account_id,
                stripe_invoice_id=invoice.stripe_invoice_id,
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
