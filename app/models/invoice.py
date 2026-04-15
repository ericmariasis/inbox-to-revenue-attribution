from datetime import datetime
import uuid

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, event
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.billing_provider import BILLING_PROVIDER_STRIPE, DEFAULT_BILLING_PROVIDER


class Invoice(Base):
    __tablename__ = "invoices"
    __table_args__ = (
        Index("ix_invoices_creator_id", "creator_id"),
        Index("ix_invoices_tid", "tid"),
        UniqueConstraint("booking_id", name="uq_invoices_booking_id"),
        UniqueConstraint(
            "payment_provider",
            "provider_account_id",
            "provider_invoice_id",
            name="uq_invoices_provider_invoice_identity",
        ),
        UniqueConstraint("stripe_invoice_id", name="uq_invoices_stripe_invoice_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    creator_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("creators.id", ondelete="CASCADE"),
        nullable=False,
    )
    booking_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("bookings.id", ondelete="CASCADE"),
        nullable=False,
    )
    tid: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("content.tid", ondelete="CASCADE"),
        nullable=False,
    )
    payment_provider: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=DEFAULT_BILLING_PROVIDER,
        server_default=DEFAULT_BILLING_PROVIDER,
    )
    provider_account_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    provider_invoice_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    provider_action_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    stripe_account_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    stripe_invoice_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="open")
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    creator = relationship("Creator", back_populates="invoices")
    booking = relationship("Booking", back_populates="invoice")
    blocked_billing_case = relationship(
        "BlockedBillingCase",
        back_populates="invoice",
        uselist=False,
    )
    content = relationship("Content", back_populates="invoices", foreign_keys=[tid])
    payment_events = relationship("InvoicePaymentEvent", back_populates="invoice")

    @property
    def resolved_payment_provider(self) -> str:
        return self.payment_provider or DEFAULT_BILLING_PROVIDER

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


def _sync_invoice_provider_identity(target: Invoice) -> None:
    if not target.payment_provider:
        target.payment_provider = DEFAULT_BILLING_PROVIDER

    if target.payment_provider != BILLING_PROVIDER_STRIPE:
        return

    if target.provider_account_id is None and target.stripe_account_id is not None:
        target.provider_account_id = target.stripe_account_id
    if target.provider_invoice_id is None and target.stripe_invoice_id is not None:
        target.provider_invoice_id = target.stripe_invoice_id

    if target.stripe_account_id is None and target.provider_account_id is not None:
        target.stripe_account_id = target.provider_account_id
    if target.stripe_invoice_id is None and target.provider_invoice_id is not None:
        target.stripe_invoice_id = target.provider_invoice_id


@event.listens_for(Invoice, "before_insert")
def _invoice_before_insert(_mapper, _connection, target: Invoice) -> None:
    _sync_invoice_provider_identity(target)


@event.listens_for(Invoice, "before_update")
def _invoice_before_update(_mapper, _connection, target: Invoice) -> None:
    _sync_invoice_provider_identity(target)
