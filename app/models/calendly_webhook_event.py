from datetime import datetime
import uuid

from sqlalchemy import JSON, DateTime, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class CalendlyWebhookEventRecord(Base):
    __tablename__ = "calendly_webhook_events"
    __table_args__ = (
        Index("ix_calendly_webhook_events_booking_uuid", "calendly_booking_uuid"),
        Index("ix_calendly_webhook_events_event_type", "event_type"),
        Index("ix_calendly_webhook_events_processing_status", "processing_status"),
        UniqueConstraint(
            "provider_event_type",
            "calendly_event_id",
            "calendly_booking_uuid",
            name="uq_calendly_webhook_events_provider_type_event_booking",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    calendly_event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    provider_event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    calendly_event_id_path: Mapped[str] = mapped_column(String(128), nullable=False)
    calendly_booking_uuid: Mapped[str] = mapped_column(String(255), nullable=False)
    calendly_booking_uuid_path: Mapped[str] = mapped_column(String(128), nullable=False)
    tid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tid_path: Mapped[str | None] = mapped_column(String(128), nullable=True)
    payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    delivery_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    processing_status: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        server_default="received",
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    last_received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
