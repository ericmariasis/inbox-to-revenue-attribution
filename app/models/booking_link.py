from datetime import datetime
import uuid

from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


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
    calendly_url: Mapped[str] = mapped_column(String(2048), nullable=False)
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
