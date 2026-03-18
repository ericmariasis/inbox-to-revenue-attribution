from datetime import datetime
import uuid

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    event,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.booking_provider import BOOKING_PROVIDER_CALENDLY


class Booking(Base):
    __tablename__ = "bookings"
    __table_args__ = (
        Index("ix_bookings_creator_id", "creator_id"),
        Index("ix_bookings_booking_link_id", "booking_link_id"),
        Index("ix_bookings_tid", "tid"),
        Index("ix_bookings_attribution_status", "attribution_status"),
        CheckConstraint(
            "("
            "(attribution_status = 'attributed' AND tid IS NOT NULL AND unattributed_reason IS NULL)"
            " OR "
            "(attribution_status = 'unattributed' AND tid IS NULL AND unattributed_reason IS NOT NULL)"
            ")",
            name="ck_bookings_attribution_current_state",
        ),
        UniqueConstraint("calendly_booking_uuid", name="uq_bookings_calendly_booking_uuid"),
        UniqueConstraint(
            "provider",
            "provider_booking_id",
            name="uq_bookings_provider_provider_booking_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    creator_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("creators.id", ondelete="CASCADE"),
        nullable=False,
    )
    tid: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("content.tid", ondelete="CASCADE"),
        nullable=True,
    )
    booking_link_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("booking_links.id", ondelete="CASCADE"),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=BOOKING_PROVIDER_CALENDLY,
        server_default=BOOKING_PROVIDER_CALENDLY,
    )
    provider_booking_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    calendly_booking_uuid: Mapped[str | None] = mapped_column(String(255), nullable=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        server_default="created",
    )
    attribution_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="attributed",
        server_default="attributed",
    )
    unattributed_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    frozen_billing_amount_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    frozen_billing_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    booked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    creator = relationship("Creator", back_populates="bookings")
    booking_link = relationship("BookingLink", back_populates="bookings")
    content = relationship("Content", back_populates="bookings", foreign_keys=[tid])
    invoice = relationship(
        "Invoice",
        back_populates="booking",
        uselist=False,
        cascade="all, delete-orphan",
    )
    blocked_billing_case = relationship(
        "BlockedBillingCase",
        back_populates="booking",
        uselist=False,
    )
    invoice_payment_events = relationship("InvoicePaymentEvent", back_populates="booking")

    @property
    def resolved_provider_booking_id(self) -> str | None:
        return self.provider_booking_id or self.calendly_booking_uuid


def _sync_booking_provider_fields(target: Booking) -> None:
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


@event.listens_for(Booking, "before_insert")
def _booking_before_insert(_mapper, _connection, target: Booking) -> None:
    _sync_booking_provider_fields(target)


@event.listens_for(Booking, "before_update")
def _booking_before_update(_mapper, _connection, target: Booking) -> None:
    _sync_booking_provider_fields(target)
