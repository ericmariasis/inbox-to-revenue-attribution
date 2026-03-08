from datetime import datetime
import uuid

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class InvoicePaymentEvent(Base):
    __tablename__ = "invoice_payment_events"
    __table_args__ = (
        Index("ix_invoice_payment_events_invoice_id", "invoice_id"),
        Index("ix_invoice_payment_events_creator_id", "creator_id"),
        Index("ix_invoice_payment_events_booking_id", "booking_id"),
        Index("ix_invoice_payment_events_tid", "tid"),
        Index("ix_invoice_payment_events_stripe_invoice_id", "stripe_invoice_id"),
        UniqueConstraint("stripe_event_id", name="uq_invoice_payment_events_stripe_event_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    stripe_event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    stripe_event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    stripe_account_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    stripe_invoice_id: Mapped[str] = mapped_column(String(255), nullable=False)
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
