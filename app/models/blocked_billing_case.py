from datetime import datetime
import uuid

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, event
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.billing_provider import BILLING_PROVIDER_STRIPE, DEFAULT_BILLING_PROVIDER
from app.models.booking_provider import BOOKING_PROVIDER_CALENDLY


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
    provider: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=BOOKING_PROVIDER_CALENDLY,
        server_default=BOOKING_PROVIDER_CALENDLY,
    )
    provider_booking_id: Mapped[str] = mapped_column(String(255), nullable=False)
    calendly_booking_uuid: Mapped[str | None] = mapped_column(String(255), nullable=True)
    payment_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    provider_account_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
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

    @property
    def resolved_provider(self) -> str:
        return self.provider or BOOKING_PROVIDER_CALENDLY

    @property
    def resolved_provider_booking_id(self) -> str | None:
        return self.provider_booking_id or self.calendly_booking_uuid

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


def _sync_blocked_billing_provider_fields(target: BlockedBillingCase) -> None:
    if not target.provider:
        target.provider = BOOKING_PROVIDER_CALENDLY

    if target.provider_booking_id is None and target.calendly_booking_uuid:
        target.provider_booking_id = target.calendly_booking_uuid

    if (
        target.provider == BOOKING_PROVIDER_CALENDLY
        and target.calendly_booking_uuid is None
        and target.provider_booking_id
    ):
        target.calendly_booking_uuid = target.provider_booking_id

    if not target.payment_provider:
        target.payment_provider = DEFAULT_BILLING_PROVIDER

    if target.payment_provider != BILLING_PROVIDER_STRIPE:
        return

    if target.provider_account_id is None and target.stripe_account_id is not None:
        target.provider_account_id = target.stripe_account_id
    if target.stripe_account_id is None and target.provider_account_id is not None:
        target.stripe_account_id = target.provider_account_id


@event.listens_for(BlockedBillingCase, "before_insert")
def _blocked_billing_case_before_insert(_mapper, _connection, target: BlockedBillingCase) -> None:
    _sync_blocked_billing_provider_fields(target)


@event.listens_for(BlockedBillingCase, "before_update")
def _blocked_billing_case_before_update(_mapper, _connection, target: BlockedBillingCase) -> None:
    _sync_blocked_billing_provider_fields(target)
