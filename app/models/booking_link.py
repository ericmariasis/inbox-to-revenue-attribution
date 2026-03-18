from datetime import datetime
import uuid

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, event, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.booking_provider import BOOKING_PROVIDER_CALENDLY


class BookingLink(Base):
    __tablename__ = "booking_links"
    __table_args__ = (Index("ix_booking_links_creator_id", "creator_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    creator_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("creators.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    provider: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=BOOKING_PROVIDER_CALENDLY,
        server_default=BOOKING_PROVIDER_CALENDLY,
    )
    destination_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    calendly_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    billing_amount_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    billing_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    creator = relationship("Creator", back_populates="booking_links")
    bookings = relationship(
        "Booking",
        back_populates="booking_link",
        cascade="all, delete-orphan",
    )
    content_items = relationship(
        "Content",
        back_populates="booking_link",
        cascade="all, delete-orphan",
    )

    @property
    def resolved_destination_url(self) -> str | None:
        return self.destination_url or self.calendly_url


def _sync_booking_link_provider_fields(target: BookingLink) -> None:
    if not target.provider:
        target.provider = BOOKING_PROVIDER_CALENDLY

    if target.destination_url is None and target.calendly_url:
        target.destination_url = target.calendly_url

    if (
        target.provider == BOOKING_PROVIDER_CALENDLY
        and target.calendly_url is None
        and target.destination_url
    ):
        target.calendly_url = target.destination_url


@event.listens_for(BookingLink, "before_insert")
def _booking_link_before_insert(_mapper, _connection, target: BookingLink) -> None:
    _sync_booking_link_provider_fields(target)


@event.listens_for(BookingLink, "before_update")
def _booking_link_before_update(_mapper, _connection, target: BookingLink) -> None:
    _sync_booking_link_provider_fields(target)
