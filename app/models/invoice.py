from datetime import datetime
import uuid

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class Invoice(Base):
    __tablename__ = "invoices"
    __table_args__ = (
        Index("ix_invoices_creator_id", "creator_id"),
        Index("ix_invoices_tid", "tid"),
        UniqueConstraint("booking_id", name="uq_invoices_booking_id"),
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
    stripe_account_id: Mapped[str] = mapped_column(String(255), nullable=False)
    stripe_invoice_id: Mapped[str] = mapped_column(String(255), nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="open")
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    creator = relationship("Creator", back_populates="invoices")
    booking = relationship("Booking", back_populates="invoice")
    content = relationship("Content", back_populates="invoices", foreign_keys=[tid])
    payment_events = relationship("InvoicePaymentEvent", back_populates="invoice")
