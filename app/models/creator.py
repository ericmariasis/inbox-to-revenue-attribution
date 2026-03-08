from datetime import datetime
import uuid

from sqlalchemy import DateTime, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class Creator(Base):
    __tablename__ = "creators"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    stripe_connect_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        server_default="pending",
    )
    stripe_connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stripe_account_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    auth_user = relationship("AuthUser", back_populates="creator", uselist=False, cascade="all, delete-orphan")
    booking_links = relationship(
        "BookingLink",
        back_populates="creator",
        cascade="all, delete-orphan",
    )
    bookings = relationship(
        "Booking",
        back_populates="creator",
        cascade="all, delete-orphan",
    )
    invoices = relationship(
        "Invoice",
        back_populates="creator",
        cascade="all, delete-orphan",
    )
    invoice_payment_events = relationship("InvoicePaymentEvent", back_populates="creator")
    content_items = relationship(
        "Content",
        back_populates="creator",
        cascade="all, delete-orphan",
    )
