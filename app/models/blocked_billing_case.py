from datetime import datetime
import uuid

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class BlockedBillingCase(Base):
    __tablename__ = "blocked_billing_cases"
    __table_args__ = (
        Index("ix_blocked_billing_cases_creator_id", "creator_id"),
        Index("ix_blocked_billing_cases_status", "status"),
        Index("ix_blocked_billing_cases_tid", "tid"),
        UniqueConstraint("booking_id", name="uq_blocked_billing_cases_booking_id"),
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
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("invoices.id", ondelete="SET NULL"),
        nullable=True,
    )
    tid: Mapped[str] = mapped_column(String(64), nullable=False)
    calendly_booking_uuid: Mapped[str] = mapped_column(String(255), nullable=False)
    stripe_account_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    frozen_amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    frozen_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="open")
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_operation: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider_http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    provider_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    first_blocked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_blocked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution_code: Mapped[str | None] = mapped_column(String(64), nullable=True)

    creator = relationship("Creator", back_populates="blocked_billing_cases")
    booking = relationship("Booking", back_populates="blocked_billing_case")
    invoice = relationship("Invoice", back_populates="blocked_billing_case")
