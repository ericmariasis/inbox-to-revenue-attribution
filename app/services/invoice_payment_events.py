import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable, Literal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.booking import Booking
from app.models.billing_provider import BILLING_PROVIDER_STRIPE
from app.models.creator import Creator
from app.models.invoice import Invoice
from app.models.invoice_payment_event import InvoicePaymentEvent


UNATTRIBUTED_REASON_MISSING_TID = "MISSING_TID"
UNATTRIBUTED_REASON_UNKNOWN_BOOKING_UUID = "UNKNOWN_BOOKING_UUID"
UNATTRIBUTED_REASON_UNKNOWN_STRIPE_INVOICE_ID = "UNKNOWN_STRIPE_INVOICE_ID"
ATTRIBUTED_PAYMENT_EVENT_STATUSES = ("applied", "reconciled")
PAYMENT_PROVENANCE_STATUS_MATCHED = "matched"
PAYMENT_PROVENANCE_STATUS_PENDING = "pending"
PAYMENT_PROVENANCE_CONFLICT_STATUS_NONE = "none"
PAYMENT_PROVENANCE_CONFLICT_STATUS_UNMATCHED_PROVIDER_SIGNAL = "unmatched_provider_signal"
PAYMENT_PROVENANCE_STATE_MATCHED = PAYMENT_PROVENANCE_STATUS_MATCHED
PAYMENT_PROVENANCE_STATE_PENDING = PAYMENT_PROVENANCE_STATUS_PENDING
PAYMENT_PROVENANCE_STATE_UNMATCHED = "unmatched"
PAYMENT_PROVENANCE_STATE_CONFLICTING = "conflicting"

InvoicePaidHandleOutcome = Literal[
    "applied",
    "duplicate",
    "noop_already_paid",
    "noop_non_open",
    "reconciled",
    "unmatched",
]
ReconcileOutcome = Literal["already_reconciled", "missing", "pending", "reconciled"]
ReconcileReason = Literal["invoice_not_found", "invoice_not_open", "missing_stripe_account_id", "payment_event_not_found"]
PaymentProvenanceStatus = Literal["matched", "pending"]
PaymentProvenanceConflictStatus = Literal["none", "unmatched_provider_signal"]
PaymentProvenanceState = Literal["matched", "pending", "unmatched", "conflicting"]


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class InvoicePaidEventHints:
    booking_uuid: str | None = None
    tid: str | None = None


@dataclass(frozen=True)
class InvoicePaidHandleResult:
    outcome: InvoicePaidHandleOutcome
    invoice_id: uuid.UUID | None = None
    payment_event_id: uuid.UUID | None = None
    creator_id: uuid.UUID | None = None
    booking_id: uuid.UUID | None = None
    booking_uuid: str | None = None
    tid: str | None = None
    invoice_status: str | None = None
    unattributed_reason: str | None = None


@dataclass(frozen=True)
class InvoicePaymentReconciliationResult:
    outcome: ReconcileOutcome
    reason: ReconcileReason | None = None
    invoice_id: uuid.UUID | None = None
    payment_event_id: uuid.UUID | None = None
    creator_id: uuid.UUID | None = None
    booking_id: uuid.UUID | None = None
    booking_uuid: str | None = None
    tid: str | None = None
    invoice_status: str | None = None


@dataclass(frozen=True)
class PaymentProvenanceSummary:
    status: PaymentProvenanceStatus
    conflict_status: PaymentProvenanceConflictStatus
    conflict_event_count: int
    conflict_reasons: tuple[str | None, ...]

    @property
    def state(self) -> PaymentProvenanceState:
        if self.conflict_status == PAYMENT_PROVENANCE_CONFLICT_STATUS_NONE:
            return self.status
        if self.status == PAYMENT_PROVENANCE_STATUS_MATCHED:
            return PAYMENT_PROVENANCE_STATE_CONFLICTING
        return PAYMENT_PROVENANCE_STATE_UNMATCHED


@dataclass(frozen=True)
class PaidRevenueByTid:
    tid: str
    amount_cents: int


@dataclass(frozen=True)
class UnattributedRevenueSummary:
    reason: str | None
    amount_cents: int
    event_count: int


@dataclass(frozen=True)
class CreatorPaidRevenueSummary:
    creator_id: uuid.UUID
    attributed_revenue_by_tid: list[PaidRevenueByTid]
    attributed_total_cents: int
    unattributed_revenue_by_reason: list[UnattributedRevenueSummary]
    unattributed_total_cents: int
    unattributed_event_count: int


@dataclass(frozen=True)
class UnmatchedPaymentEventSummary:
    payment_event_id: uuid.UUID
    payment_provider: str
    provider_event_id: str
    provider_event_type: str
    provider_account_id: str | None
    provider_invoice_id: str
    creator_id: uuid.UUID | None
    booking_id: uuid.UUID | None
    booking_uuid: str | None
    tid: str | None
    status: str
    unattributed_reason: str | None
    paid_at: datetime | None
    received_at: datetime
    processed_at: datetime | None

    @property
    def stripe_event_id(self) -> str | None:
        if self.payment_provider == BILLING_PROVIDER_STRIPE:
            return self.provider_event_id
        return None

    @property
    def stripe_event_type(self) -> str | None:
        if self.payment_provider == BILLING_PROVIDER_STRIPE:
            return self.provider_event_type
        return None

    @property
    def stripe_account_id(self) -> str | None:
        if self.payment_provider == BILLING_PROVIDER_STRIPE:
            return self.provider_account_id
        return None

    @property
    def stripe_invoice_id(self) -> str | None:
        if self.payment_provider == BILLING_PROVIDER_STRIPE:
            return self.provider_invoice_id
        return None


class InvoicePaymentEventService:
    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        now_fn: Callable[[], datetime] | None = None,
    ):
        self._session_factory = session_factory
        self._now_fn = now_fn or _utc_now

    def handle_invoice_paid_event(
        self,
        *,
        stripe_event_id: str,
        stripe_event_type: str,
        stripe_account_id: str,
        stripe_invoice_id: str,
        paid_at: datetime,
        received_at: datetime | None = None,
        hints: InvoicePaidEventHints | None = None,
    ) -> InvoicePaidHandleResult:
        return self.handle_provider_invoice_paid_event(
            payment_provider=BILLING_PROVIDER_STRIPE,
            provider_event_id=stripe_event_id,
            provider_event_type=stripe_event_type,
            provider_account_id=stripe_account_id,
            provider_invoice_id=stripe_invoice_id,
            paid_at=paid_at,
            received_at=received_at,
            hints=hints,
        )

    def handle_provider_invoice_paid_event(
        self,
        *,
        payment_provider: str,
        provider_event_id: str,
        provider_event_type: str,
        provider_account_id: str | None,
        provider_invoice_id: str,
        paid_at: datetime,
        received_at: datetime | None = None,
        hints: InvoicePaidEventHints | None = None,
    ) -> InvoicePaidHandleResult:
        resolved_received_at = received_at or self._now_fn()
        resolved_hints = hints or InvoicePaidEventHints()

        with self._session_factory() as session:
            existing_payment_event = _find_payment_event_by_provider_identity(
                session=session,
                payment_provider=payment_provider,
                provider_event_id=provider_event_id,
            )
            if existing_payment_event is not None:
                if existing_payment_event.status == "unmatched":
                    reconciliation = self.reconcile_provider_unmatched_payment_event(
                        payment_provider=payment_provider,
                        provider_event_id=provider_event_id,
                    )
                    if reconciliation.outcome == "reconciled":
                        return InvoicePaidHandleResult(
                            outcome="reconciled",
                            invoice_id=reconciliation.invoice_id,
                            payment_event_id=reconciliation.payment_event_id,
                            creator_id=reconciliation.creator_id,
                            booking_id=reconciliation.booking_id,
                            booking_uuid=reconciliation.booking_uuid,
                            tid=reconciliation.tid,
                            invoice_status=reconciliation.invoice_status,
                        )

                return InvoicePaidHandleResult(
                    outcome="duplicate",
                    invoice_id=existing_payment_event.invoice_id,
                    payment_event_id=existing_payment_event.id,
                    creator_id=existing_payment_event.creator_id,
                    booking_id=existing_payment_event.booking_id,
                    booking_uuid=(
                        existing_payment_event.booking.calendly_booking_uuid
                        if existing_payment_event.booking is not None
                        else resolved_hints.booking_uuid
                    ),
                    tid=existing_payment_event.tid,
                    invoice_status=(
                        existing_payment_event.invoice.status
                        if existing_payment_event.invoice is not None
                        else None
                    ),
                    unattributed_reason=existing_payment_event.unattributed_reason,
                )

            invoice = _find_invoice_by_provider_identity(
                session=session,
                payment_provider=payment_provider,
                provider_account_id=provider_account_id,
                provider_invoice_id=provider_invoice_id,
                with_for_update=True,
            )
            if invoice is None:
                return self._persist_unmatched_payment_event(
                    session=session,
                    payment_provider=payment_provider,
                    provider_event_id=provider_event_id,
                    provider_event_type=provider_event_type,
                    provider_account_id=provider_account_id,
                    provider_invoice_id=provider_invoice_id,
                    paid_at=paid_at,
                    received_at=resolved_received_at,
                    hints=resolved_hints,
                )

            if invoice.status == "paid":
                return InvoicePaidHandleResult(
                    outcome="noop_already_paid",
                    invoice_id=invoice.id,
                    creator_id=invoice.creator_id,
                    booking_id=invoice.booking_id,
                    booking_uuid=invoice.booking.calendly_booking_uuid,
                    tid=invoice.tid,
                    invoice_status=invoice.status,
                )

            if invoice.status != "open":
                return InvoicePaidHandleResult(
                    outcome="noop_non_open",
                    invoice_id=invoice.id,
                    creator_id=invoice.creator_id,
                    booking_id=invoice.booking_id,
                    booking_uuid=invoice.booking.calendly_booking_uuid,
                    tid=invoice.tid,
                    invoice_status=invoice.status,
                )

            invoice.status = "paid"
            invoice.paid_at = paid_at
            payment_event = InvoicePaymentEvent(
                payment_provider=payment_provider,
                provider_event_id=provider_event_id,
                provider_event_type=provider_event_type,
                provider_account_id=provider_account_id,
                provider_invoice_id=provider_invoice_id,
                invoice_id=invoice.id,
                creator_id=invoice.creator_id,
                booking_id=invoice.booking_id,
                tid=invoice.tid,
                status="applied",
                paid_at=paid_at,
                received_at=resolved_received_at,
                processed_at=resolved_received_at,
            )
            session.add(payment_event)

            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                persisted_payment_event = _find_payment_event_by_provider_identity(
                    session=session,
                    payment_provider=payment_provider,
                    provider_event_id=provider_event_id,
                )
                return InvoicePaidHandleResult(
                    outcome="duplicate",
                    invoice_id=(
                        persisted_payment_event.invoice_id
                        if persisted_payment_event is not None
                        else invoice.id
                    ),
                    payment_event_id=(
                        persisted_payment_event.id if persisted_payment_event is not None else None
                    ),
                    creator_id=(
                        persisted_payment_event.creator_id
                        if persisted_payment_event is not None
                        else invoice.creator_id
                    ),
                    booking_id=(
                        persisted_payment_event.booking_id
                        if persisted_payment_event is not None
                        else invoice.booking_id
                    ),
                    booking_uuid=(
                        persisted_payment_event.booking.calendly_booking_uuid
                        if persisted_payment_event is not None
                        and persisted_payment_event.booking is not None
                        else invoice.booking.calendly_booking_uuid
                    ),
                    tid=(
                        persisted_payment_event.tid
                        if persisted_payment_event is not None
                        else invoice.tid
                    ),
                    invoice_status=invoice.status,
                    unattributed_reason=(
                        persisted_payment_event.unattributed_reason
                        if persisted_payment_event is not None
                        else None
                    ),
                )

            return InvoicePaidHandleResult(
                outcome="applied",
                invoice_id=invoice.id,
                payment_event_id=payment_event.id,
                creator_id=invoice.creator_id,
                booking_id=invoice.booking_id,
                booking_uuid=invoice.booking.calendly_booking_uuid,
                tid=invoice.tid,
                invoice_status=invoice.status,
            )

    def reconcile_unmatched_payment_event(
        self,
        *,
        stripe_event_id: str,
    ) -> InvoicePaymentReconciliationResult:
        return self.reconcile_provider_unmatched_payment_event(
            payment_provider=BILLING_PROVIDER_STRIPE,
            provider_event_id=stripe_event_id,
        )

    def reconcile_provider_unmatched_payment_event(
        self,
        *,
        payment_provider: str,
        provider_event_id: str,
    ) -> InvoicePaymentReconciliationResult:
        with self._session_factory() as session:
            payment_event = _find_payment_event_by_provider_identity(
                session=session,
                payment_provider=payment_provider,
                provider_event_id=provider_event_id,
                with_for_update=True,
            )
            if payment_event is None:
                return InvoicePaymentReconciliationResult(
                    outcome="missing",
                    reason="payment_event_not_found",
                )

            if payment_event.status in ATTRIBUTED_PAYMENT_EVENT_STATUSES and payment_event.invoice_id is not None:
                return InvoicePaymentReconciliationResult(
                    outcome="already_reconciled",
                    invoice_id=payment_event.invoice_id,
                    payment_event_id=payment_event.id,
                    creator_id=payment_event.creator_id,
                    booking_id=payment_event.booking_id,
                    booking_uuid=(
                        payment_event.booking.calendly_booking_uuid
                        if payment_event.booking is not None
                        else None
                    ),
                    tid=payment_event.tid,
                    invoice_status=(
                        payment_event.invoice.status if payment_event.invoice is not None else None
                    ),
                )

            if payment_event.resolved_provider_account_id is None:
                return InvoicePaymentReconciliationResult(
                    outcome="pending",
                    reason="missing_stripe_account_id",
                    payment_event_id=payment_event.id,
                    creator_id=payment_event.creator_id,
                    booking_id=payment_event.booking_id,
                    booking_uuid=(
                        payment_event.booking.calendly_booking_uuid
                        if payment_event.booking is not None
                        else None
                    ),
                    tid=payment_event.tid,
                )

            invoice = _find_invoice_by_provider_identity(
                session=session,
                payment_provider=payment_event.resolved_payment_provider,
                provider_account_id=payment_event.resolved_provider_account_id,
                provider_invoice_id=payment_event.resolved_provider_invoice_id,
                with_for_update=True,
            )
            if invoice is None:
                return InvoicePaymentReconciliationResult(
                    outcome="pending",
                    reason="invoice_not_found",
                    payment_event_id=payment_event.id,
                    creator_id=payment_event.creator_id,
                    booking_id=payment_event.booking_id,
                    booking_uuid=(
                        payment_event.booking.calendly_booking_uuid
                        if payment_event.booking is not None
                        else None
                    ),
                    tid=payment_event.tid,
                )

            if invoice.status not in {"open", "paid"}:
                return InvoicePaymentReconciliationResult(
                    outcome="pending",
                    reason="invoice_not_open",
                    invoice_id=invoice.id,
                    payment_event_id=payment_event.id,
                    creator_id=payment_event.creator_id,
                    booking_id=payment_event.booking_id,
                    booking_uuid=(
                        payment_event.booking.calendly_booking_uuid
                        if payment_event.booking is not None
                        else None
                    ),
                    tid=payment_event.tid,
                    invoice_status=invoice.status,
                )

            if invoice.status == "open":
                invoice.status = "paid"
                if invoice.paid_at is None:
                    invoice.paid_at = payment_event.paid_at or self._now_fn()

            payment_event.invoice_id = invoice.id
            payment_event.creator_id = invoice.creator_id
            payment_event.booking_id = invoice.booking_id
            payment_event.tid = invoice.tid
            payment_event.status = "reconciled"
            payment_event.unattributed_reason = None
            payment_event.processed_at = self._now_fn()
            session.commit()

            return InvoicePaymentReconciliationResult(
                outcome="reconciled",
                invoice_id=invoice.id,
                payment_event_id=payment_event.id,
                creator_id=invoice.creator_id,
                booking_id=invoice.booking_id,
                booking_uuid=invoice.booking.calendly_booking_uuid,
                tid=invoice.tid,
                invoice_status=invoice.status,
            )

    def summarize_paid_revenue(
        self,
        *,
        creator_id: uuid.UUID,
    ) -> CreatorPaidRevenueSummary:
        with self._session_factory() as session:
            attributed_rows = session.execute(
                select(
                    InvoicePaymentEvent.tid,
                    func.coalesce(func.sum(Invoice.amount_cents), 0),
                )
                .join(Invoice, Invoice.id == InvoicePaymentEvent.invoice_id)
                .where(
                    InvoicePaymentEvent.creator_id == creator_id,
                    InvoicePaymentEvent.status.in_(ATTRIBUTED_PAYMENT_EVENT_STATUSES),
                    InvoicePaymentEvent.tid.is_not(None),
                )
                .group_by(InvoicePaymentEvent.tid)
                .order_by(InvoicePaymentEvent.tid.asc())
            ).all()

            unattributed_rows = session.execute(
                select(
                    InvoicePaymentEvent.unattributed_reason,
                    func.coalesce(func.sum(Invoice.amount_cents), 0),
                    func.count(InvoicePaymentEvent.id),
                )
                .outerjoin(Invoice, Invoice.id == InvoicePaymentEvent.invoice_id)
                .where(
                    InvoicePaymentEvent.creator_id == creator_id,
                    InvoicePaymentEvent.status == "unmatched",
                )
                .group_by(InvoicePaymentEvent.unattributed_reason)
                .order_by(InvoicePaymentEvent.unattributed_reason.asc())
            ).all()

        attributed_revenue_by_tid = [
            PaidRevenueByTid(tid=tid, amount_cents=amount_cents)
            for tid, amount_cents in attributed_rows
            if tid is not None
        ]
        unattributed_revenue_by_reason = [
            UnattributedRevenueSummary(
                reason=reason,
                amount_cents=amount_cents,
                event_count=event_count,
            )
            for reason, amount_cents, event_count in unattributed_rows
        ]

        return CreatorPaidRevenueSummary(
            creator_id=creator_id,
            attributed_revenue_by_tid=attributed_revenue_by_tid,
            attributed_total_cents=sum(item.amount_cents for item in attributed_revenue_by_tid),
            unattributed_revenue_by_reason=unattributed_revenue_by_reason,
            unattributed_total_cents=sum(item.amount_cents for item in unattributed_revenue_by_reason),
            unattributed_event_count=sum(item.event_count for item in unattributed_revenue_by_reason),
        )

    def _persist_unmatched_payment_event(
        self,
        *,
        session: Session,
        payment_provider: str,
        provider_event_id: str,
        provider_event_type: str,
        provider_account_id: str | None,
        provider_invoice_id: str,
        paid_at: datetime,
        received_at: datetime,
        hints: InvoicePaidEventHints,
    ) -> InvoicePaidHandleResult:
        creator = _find_creator_by_provider_account(
            session=session,
            payment_provider=payment_provider,
            provider_account_id=provider_account_id,
        )
        booking = _find_booking_for_account(
            session=session,
            payment_provider=payment_provider,
            provider_account_id=provider_account_id,
            booking_uuid=hints.booking_uuid,
        )
        unattributed_reason = _resolve_unattributed_reason(
            booking=booking,
            hints=hints,
        )
        payment_event = InvoicePaymentEvent(
            payment_provider=payment_provider,
            provider_event_id=provider_event_id,
            provider_event_type=provider_event_type,
            provider_account_id=provider_account_id,
            provider_invoice_id=provider_invoice_id,
            invoice_id=None,
            creator_id=booking.creator_id if booking is not None else (creator.id if creator is not None else None),
            booking_id=booking.id if booking is not None else None,
            tid=booking.tid if booking is not None else None,
            status="unmatched",
            unattributed_reason=unattributed_reason,
            paid_at=paid_at,
            received_at=received_at,
            processed_at=None,
        )
        session.add(payment_event)

        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            persisted_payment_event = _find_payment_event_by_provider_identity(
                session=session,
                payment_provider=payment_provider,
                provider_event_id=provider_event_id,
            )
            return InvoicePaidHandleResult(
                outcome="duplicate",
                invoice_id=(
                    persisted_payment_event.invoice_id if persisted_payment_event is not None else None
                ),
                payment_event_id=(
                    persisted_payment_event.id if persisted_payment_event is not None else None
                ),
                creator_id=(
                    persisted_payment_event.creator_id if persisted_payment_event is not None else None
                ),
                booking_id=(
                    persisted_payment_event.booking_id if persisted_payment_event is not None else None
                ),
                booking_uuid=(
                    persisted_payment_event.booking.calendly_booking_uuid
                    if persisted_payment_event is not None
                    and persisted_payment_event.booking is not None
                    else hints.booking_uuid
                ),
                tid=(
                    persisted_payment_event.tid if persisted_payment_event is not None else None
                ),
                unattributed_reason=(
                    persisted_payment_event.unattributed_reason
                    if persisted_payment_event is not None
                    else unattributed_reason
                ),
            )

        return InvoicePaidHandleResult(
            outcome="unmatched",
            payment_event_id=payment_event.id,
            creator_id=payment_event.creator_id,
            booking_id=payment_event.booking_id,
            booking_uuid=booking.calendly_booking_uuid if booking is not None else hints.booking_uuid,
            tid=payment_event.tid,
            unattributed_reason=unattributed_reason,
        )


def build_payment_provenance_summary(
    *,
    payment_event_status: str | None,
    conflict_event_count: int = 0,
    conflict_reasons: Sequence[str | None] = (),
) -> PaymentProvenanceSummary:
    status: PaymentProvenanceStatus = (
        PAYMENT_PROVENANCE_STATUS_MATCHED
        if payment_event_status in ATTRIBUTED_PAYMENT_EVENT_STATUSES
        else PAYMENT_PROVENANCE_STATUS_PENDING
    )
    if conflict_event_count <= 0:
        return PaymentProvenanceSummary(
            status=status,
            conflict_status=PAYMENT_PROVENANCE_CONFLICT_STATUS_NONE,
            conflict_event_count=0,
            conflict_reasons=(),
        )

    return PaymentProvenanceSummary(
        status=status,
        conflict_status=PAYMENT_PROVENANCE_CONFLICT_STATUS_UNMATCHED_PROVIDER_SIGNAL,
        conflict_event_count=conflict_event_count,
        conflict_reasons=tuple(conflict_reasons),
    )


def list_current_unmatched_payment_events(
    *,
    creator_id: uuid.UUID,
    db: Session,
) -> list[UnmatchedPaymentEventSummary]:
    rows = db.execute(
        select(
            InvoicePaymentEvent,
            Booking.calendly_booking_uuid,
        )
        .select_from(InvoicePaymentEvent)
        .outerjoin(Booking, Booking.id == InvoicePaymentEvent.booking_id)
        .where(
            InvoicePaymentEvent.creator_id == creator_id,
            InvoicePaymentEvent.status == "unmatched",
        )
        .order_by(
            InvoicePaymentEvent.received_at.desc(),
            func.coalesce(
                InvoicePaymentEvent.provider_event_id,
                InvoicePaymentEvent.stripe_event_id,
            ).asc(),
        )
    ).all()

    return [
        UnmatchedPaymentEventSummary(
            payment_event_id=payment_event.id,
            payment_provider=payment_event.resolved_payment_provider,
            provider_event_id=payment_event.resolved_provider_event_id or "missing",
            provider_event_type=payment_event.resolved_provider_event_type or "unknown",
            provider_account_id=payment_event.resolved_provider_account_id,
            provider_invoice_id=payment_event.resolved_provider_invoice_id or "missing",
            creator_id=payment_event.creator_id,
            booking_id=payment_event.booking_id,
            booking_uuid=booking_uuid,
            tid=payment_event.tid,
            status=payment_event.status,
            unattributed_reason=payment_event.unattributed_reason,
            paid_at=payment_event.paid_at,
            received_at=payment_event.received_at,
            processed_at=payment_event.processed_at,
        )
        for payment_event, booking_uuid in rows
    ]


def _find_payment_event_by_provider_identity(
    *,
    session: Session,
    payment_provider: str,
    provider_event_id: str,
    with_for_update: bool = False,
) -> InvoicePaymentEvent | None:
    query = select(InvoicePaymentEvent).where(
        InvoicePaymentEvent.payment_provider == payment_provider,
        InvoicePaymentEvent.provider_event_id == provider_event_id,
    )
    if with_for_update:
        query = query.with_for_update()

    payment_event = session.scalar(query)
    if payment_event is not None or payment_provider != BILLING_PROVIDER_STRIPE:
        return payment_event

    legacy_query = select(InvoicePaymentEvent).where(
        InvoicePaymentEvent.stripe_event_id == provider_event_id
    )
    if with_for_update:
        legacy_query = legacy_query.with_for_update()
    return session.scalar(legacy_query)


def _find_invoice_by_provider_identity(
    *,
    session: Session,
    payment_provider: str,
    provider_account_id: str | None,
    provider_invoice_id: str | None,
    with_for_update: bool = False,
) -> Invoice | None:
    if provider_invoice_id is None:
        return None

    query = select(Invoice).where(
        Invoice.payment_provider == payment_provider,
        Invoice.provider_invoice_id == provider_invoice_id,
    )
    if provider_account_id is None:
        query = query.where(Invoice.provider_account_id.is_(None))
    else:
        query = query.where(Invoice.provider_account_id == provider_account_id)
    if with_for_update:
        query = query.with_for_update()

    invoice = session.scalar(query)
    if invoice is not None or payment_provider != BILLING_PROVIDER_STRIPE:
        return invoice

    legacy_query = select(Invoice).where(Invoice.stripe_invoice_id == provider_invoice_id)
    if provider_account_id is None:
        legacy_query = legacy_query.where(Invoice.stripe_account_id.is_(None))
    else:
        legacy_query = legacy_query.where(Invoice.stripe_account_id == provider_account_id)
    if with_for_update:
        legacy_query = legacy_query.with_for_update()
    return session.scalar(legacy_query)


def _find_creator_by_provider_account(
    *,
    session: Session,
    payment_provider: str,
    provider_account_id: str | None,
) -> Creator | None:
    if provider_account_id is None:
        return None

    creator = session.scalar(
        select(Creator).where(
            Creator.billing_provider == payment_provider,
            Creator.billing_account_id == provider_account_id,
        )
    )
    if creator is not None or payment_provider != BILLING_PROVIDER_STRIPE:
        return creator

    return session.scalar(select(Creator).where(Creator.stripe_account_id == provider_account_id))


def _find_booking_for_account(
    *,
    session: Session,
    payment_provider: str,
    provider_account_id: str | None,
    booking_uuid: str | None,
) -> Booking | None:
    if booking_uuid is None or provider_account_id is None:
        return None

    booking = session.scalar(
        select(Booking)
        .join(Creator, Creator.id == Booking.creator_id)
        .where(
            Booking.calendly_booking_uuid == booking_uuid,
            Creator.billing_provider == payment_provider,
            Creator.billing_account_id == provider_account_id,
        )
    )
    if booking is not None or payment_provider != BILLING_PROVIDER_STRIPE:
        return booking

    return session.scalar(
        select(Booking)
        .join(Creator, Creator.id == Booking.creator_id)
        .where(
            Booking.calendly_booking_uuid == booking_uuid,
            Creator.stripe_account_id == provider_account_id,
        )
    )


def _resolve_unattributed_reason(
    *,
    booking: Booking | None,
    hints: InvoicePaidEventHints,
) -> str:
    if hints.booking_uuid is not None and booking is None:
        return UNATTRIBUTED_REASON_UNKNOWN_BOOKING_UUID
    if hints.tid is None:
        return UNATTRIBUTED_REASON_MISSING_TID
    return UNATTRIBUTED_REASON_UNKNOWN_STRIPE_INVOICE_ID
