from datetime import datetime
import uuid

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint, event, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.billing_provider import BILLING_PROVIDER_STRIPE, DEFAULT_BILLING_PROVIDER


class InvoicePaymentEvent(Base):
    __tablename__ = "invoice_payment_events"
    __table_args__ = (
        Index("ix_invoice_payment_events_invoice_id", "invoice_id"),
        Index("ix_invoice_payment_events_creator_id", "creator_id"),
        Index("ix_invoice_payment_events_booking_id", "booking_id"),
        Index("ix_invoice_payment_events_tid", "tid"),
        Index("ix_invoice_payment_events_stripe_invoice_id", "stripe_invoice_id"),
        UniqueConstraint(
            "payment_provider",
            "provider_event_id",
            name="uq_invoice_payment_events_provider_event_identity",
        ),
        UniqueConstraint("stripe_event_id", name="uq_invoice_payment_events_stripe_event_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    payment_provider: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=DEFAULT_BILLING_PROVIDER,
        server_default=DEFAULT_BILLING_PROVIDER,
    )
    provider_event_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    provider_event_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider_account_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    provider_invoice_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    stripe_event_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    stripe_event_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    stripe_account_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    stripe_invoice_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("invoices.id", ondelete="SET NULL"),
        nullable=True,
    )
    creator_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("creators.id", ondelete="SET NULL"),
        nullable=True,
    )
    booking_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("bookings.id", ondelete="SET NULL"),
        nullable=True,
    )
    tid: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("content.tid", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    unattributed_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    invoice = relationship("Invoice", back_populates="payment_events")
    creator = relationship("Creator", back_populates="invoice_payment_events")
    booking = relationship("Booking", back_populates="invoice_payment_events")
    content = relationship("Content", back_populates="invoice_payment_events", foreign_keys=[tid])

    @property
    def resolved_payment_provider(self) -> str:
        return self.payment_provider or DEFAULT_BILLING_PROVIDER

    @property
    def resolved_provider_event_id(self) -> str | None:
        if self.provider_event_id is not None:
            return self.provider_event_id
        if self.resolved_payment_provider == BILLING_PROVIDER_STRIPE:
            return self.stripe_event_id
        return None

    @property
    def resolved_provider_event_type(self) -> str | None:
        if self.provider_event_type is not None:
            return self.provider_event_type
        if self.resolved_payment_provider == BILLING_PROVIDER_STRIPE:
            return self.stripe_event_type
        return None

    @property
    def resolved_provider_account_id(self) -> str | None:
        if self.provider_account_id is not None:
            return self.provider_account_id
        if self.resolved_payment_provider == BILLING_PROVIDER_STRIPE:
            return self.stripe_account_id
        return None

    @property
    def resolved_provider_invoice_id(self) -> str | None:
        if self.provider_invoice_id is not None:
            return self.provider_invoice_id
        if self.resolved_payment_provider == BILLING_PROVIDER_STRIPE:
            return self.stripe_invoice_id
        return None


def _sync_invoice_payment_event_provider_identity(target: InvoicePaymentEvent) -> None:
    if not target.payment_provider:
        target.payment_provider = DEFAULT_BILLING_PROVIDER

    if target.payment_provider != BILLING_PROVIDER_STRIPE:
        return

    if target.provider_event_id is None and target.stripe_event_id is not None:
        target.provider_event_id = target.stripe_event_id
    if target.provider_event_type is None and target.stripe_event_type is not None:
        target.provider_event_type = target.stripe_event_type
    if target.provider_account_id is None and target.stripe_account_id is not None:
        target.provider_account_id = target.stripe_account_id
    if target.provider_invoice_id is None and target.stripe_invoice_id is not None:
        target.provider_invoice_id = target.stripe_invoice_id

    if target.stripe_event_id is None and target.provider_event_id is not None:
        target.stripe_event_id = target.provider_event_id
    if target.stripe_event_type is None and target.provider_event_type is not None:
        target.stripe_event_type = target.provider_event_type
    if target.stripe_account_id is None and target.provider_account_id is not None:
        target.stripe_account_id = target.provider_account_id
    if target.stripe_invoice_id is None and target.provider_invoice_id is not None:
        target.stripe_invoice_id = target.provider_invoice_id


@event.listens_for(InvoicePaymentEvent, "before_insert")
def _invoice_payment_event_before_insert(_mapper, _connection, target: InvoicePaymentEvent) -> None:
    _sync_invoice_payment_event_provider_identity(target)


@event.listens_for(InvoicePaymentEvent, "before_update")
def _invoice_payment_event_before_update(_mapper, _connection, target: InvoicePaymentEvent) -> None:
    _sync_invoice_payment_event_provider_identity(target)
