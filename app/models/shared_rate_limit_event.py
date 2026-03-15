from datetime import datetime
import uuid

from sqlalchemy import DateTime, Index, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class SharedRateLimitEvent(Base):
    __tablename__ = "shared_rate_limit_events"
    __table_args__ = (
        Index(
            "ix_shared_rate_limit_events_namespace_bucket_observed_at",
            "namespace",
            "bucket_key",
            "observed_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    namespace: Mapped[str] = mapped_column(String(64), nullable=False)
    bucket_key: Mapped[str] = mapped_column(String(512), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
