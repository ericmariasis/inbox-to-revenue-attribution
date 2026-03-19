from datetime import datetime
import uuid

from sqlalchemy import JSON, DateTime, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class FullScopeWebhookEventRecord(Base):
    __tablename__ = "fullscope_webhook_events"
    __table_args__ = (
        Index("ix_fullscope_webhook_events_appointment_id", "appointment_id"),
        Index("ix_fullscope_webhook_events_event_type", "event_type"),
        Index("ix_fullscope_webhook_events_processing_status", "processing_status"),
        Index("ix_fullscope_webhook_events_reducer_key", "reducer_key"),
        UniqueConstraint(
            "provider_event_type",
            "appointment_id",
            "payload_sha256",
            name="uq_fullscope_webhook_events_provider_type_appointment_hash",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    provider_event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    appointment_id: Mapped[str] = mapped_column(String(255), nullable=False)
    appointment_id_path: Mapped[str] = mapped_column(String(128), nullable=False)
    calendar_id: Mapped[str] = mapped_column(String(255), nullable=False)
    calendar_id_path: Mapped[str] = mapped_column(String(128), nullable=False)
    workflow_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    workflow_id_path: Mapped[str | None] = mapped_column(String(128), nullable=True)
    tid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tid_path: Mapped[str | None] = mapped_column(String(128), nullable=True)
    payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    reducer_key: Mapped[str] = mapped_column(String(255), nullable=False)
    delivery_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    processing_status: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        server_default="received",
    )
    reducer_attempt_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default="0",
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
