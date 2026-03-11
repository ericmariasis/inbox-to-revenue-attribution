import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable, Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.blocked_billing_case import BlockedBillingCase
from app.models.booking import Booking
from app.models.invoice import Invoice
from app.services.stripe_provider import StripeProvider


logger = logging.getLogger(__name__)

BLOCKED_BILLING_CASE_STATUS_OPEN = "open"
BLOCKED_BILLING_CASE_STATUS_RESOLVED = "resolved"
BLOCKED_BILLING_REASON_CREATOR_NOT_BILLABLE = "creator_not_billable"
BLOCKED_BILLING_REASON_PROVIDER_ERROR = "provider_error"
BLOCKED_BILLING_RESOLUTION_INVOICE_CREATED = "invoice_created"
BLOCKED_BILLING_RESOLUTION_INVOICE_EXISTING = "invoice_existing"
BLOCKED_BILLING_RESOLUTION_BOOKING_CANCELED = "booking_canceled"

RetryBlockedBillingOutcome = Literal[
    "already_resolved",
    "closed",
    "created",
    "existing",
    "missing",
    "still_blocked",
]


@dataclass(frozen=True)
class BlockedBillingCaseSummary:
    case_id: uuid.UUID
    booking_id: uuid.UUID
    booking_status: str
    invoice_id: uuid.UUID | None
    invoice_status: str | None
    stripe_invoice_id: str | None
    tid: str
    calendly_booking_uuid: str
    stripe_account_id: str | None
    frozen_amount_cents: int
    frozen_currency: str
    status: str
    reason_code: str
    provider_operation: str | None
    provider_http_status: int | None
    provider_error_code: str | None
    first_blocked_at: datetime
    last_blocked_at: datetime
    last_retry_at: datetime | None
    resolved_at: datetime | None
    resolution_code: str | None


@dataclass(frozen=True)
class RetryBlockedBillingResult:
    outcome: RetryBlockedBillingOutcome
    reason_code: str | None = None
    resolution_code: str | None = None
    invoice_id: uuid.UUID | None = None
    stripe_invoice_id: str | None = None
    invoice_status: str | None = None


class BlockedBillingRetryService:
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

    def retry_case(
        self,
        *,
        case_id: uuid.UUID,
        creator_id: uuid.UUID,
    ) -> RetryBlockedBillingResult:
        retry_at = self._now_fn()

        with self._session_factory() as session:
            blocked_case = session.scalar(
                select(BlockedBillingCase)
                .where(
                    BlockedBillingCase.id == case_id,
                    BlockedBillingCase.creator_id == creator_id,
                )
                .with_for_update()
            )
            if blocked_case is None:
                return RetryBlockedBillingResult(outcome="missing")

            if blocked_case.status != BLOCKED_BILLING_CASE_STATUS_OPEN:
                return RetryBlockedBillingResult(
                    outcome="already_resolved",
                    reason_code=blocked_case.reason_code,
                    resolution_code=blocked_case.resolution_code,
                    invoice_id=blocked_case.invoice_id,
                    invoice_status=(
                        blocked_case.invoice.status if blocked_case.invoice is not None else None
                    ),
                    stripe_invoice_id=(
                        blocked_case.invoice.stripe_invoice_id
                        if blocked_case.invoice is not None
                        else None
                    ),
                )

            booking = session.get(Booking, blocked_case.booking_id)
            if booking is None or booking.status != "created":
                resolve_blocked_billing_case_for_booking_canceled(
                    session,
                    booking_id=blocked_case.booking_id,
                    resolved_at=retry_at,
                )
                session.commit()
                return RetryBlockedBillingResult(
                    outcome="closed",
                    reason_code=blocked_case.reason_code,
                    resolution_code=BLOCKED_BILLING_RESOLUTION_BOOKING_CANCELED,
                )

            blocked_case.last_retry_at = retry_at
            booking_id = blocked_case.booking_id
            if (
                booking.frozen_billing_amount_cents is None
                or booking.frozen_billing_currency is None
            ):
                booking.frozen_billing_amount_cents = blocked_case.frozen_amount_cents
                booking.frozen_billing_currency = blocked_case.frozen_currency.upper()
            session.commit()

        from app.services.billing import BillingOrchestrator

        orchestrator = BillingOrchestrator(
            session_factory=self._session_factory,
            provider=self._provider,
            now_fn=self._now_fn,
        )
        result = orchestrator.create_invoice_for_booking(booking_id=booking_id)

        if result.outcome == "created":
            return RetryBlockedBillingResult(
                outcome="created",
                invoice_id=result.invoice_id,
                stripe_invoice_id=result.stripe_invoice_id,
                invoice_status=result.invoice_status,
            )
        if result.outcome == "existing":
            return RetryBlockedBillingResult(
                outcome="existing",
                invoice_id=result.invoice_id,
                stripe_invoice_id=result.stripe_invoice_id,
                invoice_status=result.invoice_status,
            )
        if result.reason in {"booking_not_active", "booking_not_found"}:
            with self._session_factory() as session:
                resolve_blocked_billing_case_for_booking_canceled(
                    session,
                    booking_id=booking_id,
                    resolved_at=self._now_fn(),
                )
                session.commit()
            return RetryBlockedBillingResult(
                outcome="closed",
                reason_code=result.reason,
                resolution_code=BLOCKED_BILLING_RESOLUTION_BOOKING_CANCELED,
            )
        return RetryBlockedBillingResult(
            outcome="still_blocked",
            reason_code=result.reason,
            invoice_id=result.invoice_id,
            stripe_invoice_id=result.stripe_invoice_id,
            invoice_status=result.invoice_status,
        )


def record_blocked_billing_case(
    session: Session,
    *,
    booking: Booking,
    frozen_amount_cents: int,
    frozen_currency: str,
    reason_code: str,
    blocked_at: datetime,
    stripe_account_id: str | None,
    provider_operation: str | None = None,
    provider_http_status: int | None = None,
    provider_error_code: str | None = None,
) -> BlockedBillingCase:
    blocked_case = session.scalar(
        select(BlockedBillingCase)
        .where(BlockedBillingCase.booking_id == booking.id)
        .with_for_update()
    )
    created = blocked_case is None
    if blocked_case is None:
        blocked_case = BlockedBillingCase(
            creator_id=booking.creator_id,
            booking_id=booking.id,
            tid=booking.tid,
            calendly_booking_uuid=booking.calendly_booking_uuid,
            frozen_amount_cents=frozen_amount_cents,
            frozen_currency=frozen_currency.upper(),
            status=BLOCKED_BILLING_CASE_STATUS_OPEN,
            reason_code=reason_code,
            first_blocked_at=blocked_at,
            last_blocked_at=blocked_at,
        )
        session.add(blocked_case)

    blocked_case.invoice_id = None
    booking.frozen_billing_amount_cents = frozen_amount_cents
    booking.frozen_billing_currency = frozen_currency.upper()
    blocked_case.tid = booking.tid
    blocked_case.calendly_booking_uuid = booking.calendly_booking_uuid
    blocked_case.stripe_account_id = stripe_account_id
    blocked_case.frozen_amount_cents = frozen_amount_cents
    blocked_case.frozen_currency = frozen_currency.upper()
    blocked_case.status = BLOCKED_BILLING_CASE_STATUS_OPEN
    blocked_case.reason_code = reason_code
    blocked_case.provider_operation = provider_operation
    blocked_case.provider_http_status = provider_http_status
    blocked_case.provider_error_code = provider_error_code
    blocked_case.last_blocked_at = blocked_at
    blocked_case.resolved_at = None
    blocked_case.resolution_code = None

    logger.info(
        "blocked_billing_case_%s booking_id=%s creator_id=%s reason_code=%s stripe_account_id=%s",
        "created" if created else "updated",
        booking.id,
        booking.creator_id,
        reason_code,
        stripe_account_id,
    )
    return blocked_case


def resolve_blocked_billing_case_for_invoice(
    session: Session,
    *,
    booking_id: uuid.UUID,
    invoice: Invoice,
    resolved_at: datetime,
    resolution_code: str,
) -> bool:
    blocked_case = session.scalar(
        select(BlockedBillingCase)
        .where(BlockedBillingCase.booking_id == booking_id)
        .with_for_update()
    )
    if blocked_case is None:
        return False

    blocked_case.invoice_id = invoice.id
    blocked_case.status = BLOCKED_BILLING_CASE_STATUS_RESOLVED
    blocked_case.resolved_at = resolved_at
    blocked_case.resolution_code = resolution_code
    logger.info(
        "blocked_billing_case_resolved booking_id=%s invoice_id=%s resolution_code=%s",
        booking_id,
        invoice.id,
        resolution_code,
    )
    return True


def resolve_blocked_billing_case_for_booking_canceled(
    session: Session,
    *,
    booking_id: uuid.UUID,
    resolved_at: datetime,
) -> bool:
    blocked_case = session.scalar(
        select(BlockedBillingCase)
        .where(BlockedBillingCase.booking_id == booking_id)
        .with_for_update()
    )
    if blocked_case is None:
        return False

    blocked_case.status = BLOCKED_BILLING_CASE_STATUS_RESOLVED
    blocked_case.resolved_at = resolved_at
    blocked_case.resolution_code = BLOCKED_BILLING_RESOLUTION_BOOKING_CANCELED
    logger.info(
        "blocked_billing_case_closed_booking_canceled booking_id=%s case_id=%s",
        booking_id,
        blocked_case.id,
    )
    return True


def count_open_blocked_billing_cases(*, creator_id: uuid.UUID, db: Session) -> int:
    return int(
        db.scalar(
            select(func.count(BlockedBillingCase.id)).where(
                BlockedBillingCase.creator_id == creator_id,
                BlockedBillingCase.status == BLOCKED_BILLING_CASE_STATUS_OPEN,
            )
        )
        or 0
    )


def list_open_blocked_billing_cases(
    *,
    creator_id: uuid.UUID,
    db: Session,
) -> list[BlockedBillingCaseSummary]:
    rows = db.execute(
        select(
            BlockedBillingCase,
            Booking.status,
            Invoice.status,
            Invoice.stripe_invoice_id,
        )
        .select_from(BlockedBillingCase)
        .join(Booking, Booking.id == BlockedBillingCase.booking_id)
        .outerjoin(Invoice, Invoice.id == BlockedBillingCase.invoice_id)
        .where(
            BlockedBillingCase.creator_id == creator_id,
            BlockedBillingCase.status == BLOCKED_BILLING_CASE_STATUS_OPEN,
        )
        .order_by(
            BlockedBillingCase.last_blocked_at.desc(),
            BlockedBillingCase.first_blocked_at.desc(),
        )
    ).all()

    return [
        BlockedBillingCaseSummary(
            case_id=blocked_case.id,
            booking_id=blocked_case.booking_id,
            booking_status=booking_status,
            invoice_id=blocked_case.invoice_id,
            invoice_status=invoice_status,
            stripe_invoice_id=stripe_invoice_id,
            tid=blocked_case.tid,
            calendly_booking_uuid=blocked_case.calendly_booking_uuid,
            stripe_account_id=blocked_case.stripe_account_id,
            frozen_amount_cents=blocked_case.frozen_amount_cents,
            frozen_currency=blocked_case.frozen_currency,
            status=blocked_case.status,
            reason_code=blocked_case.reason_code,
            provider_operation=blocked_case.provider_operation,
            provider_http_status=blocked_case.provider_http_status,
            provider_error_code=blocked_case.provider_error_code,
            first_blocked_at=blocked_case.first_blocked_at,
            last_blocked_at=blocked_case.last_blocked_at,
            last_retry_at=blocked_case.last_retry_at,
            resolved_at=blocked_case.resolved_at,
            resolution_code=blocked_case.resolution_code,
        )
        for blocked_case, booking_status, invoice_status, stripe_invoice_id in rows
    ]


def _utc_now() -> datetime:
    return datetime.now(UTC)
