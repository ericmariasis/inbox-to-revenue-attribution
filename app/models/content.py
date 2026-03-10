from datetime import datetime
import uuid

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class Content(Base):
    __tablename__ = "content"
    __table_args__ = (
        Index("ix_content_creator_id", "creator_id"),
        Index("ix_content_booking_link_id", "booking_link_id"),
        UniqueConstraint("tid", name="uq_content_tid"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    creator_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("creators.id", ondelete="CASCADE"),
        nullable=False,
    )
    booking_link_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("booking_links.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    tid: Mapped[str] = mapped_column(String(64), nullable=False)
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

    creator = relationship("Creator", back_populates="content_items")
    bookings = relationship(
        "Booking",
        back_populates="content",
        cascade="all, delete-orphan",
        foreign_keys="Booking.tid",
    )
    invoices = relationship(
        "Invoice",
        back_populates="content",
        cascade="all, delete-orphan",
        foreign_keys="Invoice.tid",
    )
    invoice_payment_events = relationship(
        "InvoicePaymentEvent",
        back_populates="content",
        foreign_keys="InvoicePaymentEvent.tid",
    )
    fetch_snapshots = relationship(
        "ContentFetchSnapshot",
        back_populates="content",
        cascade="all, delete-orphan",
    )
    extraction_artifacts = relationship(
        "ContentExtractionArtifact",
        back_populates="content",
        cascade="all, delete-orphan",
    )
    booking_link = relationship("BookingLink", back_populates="content_items")
