from datetime import datetime
import uuid

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.billing_provider import DEFAULT_BILLING_CONNECT_STATUS


class BillingProviderSwitchAttempt(Base):
    __tablename__ = "billing_provider_switch_attempts"
    __table_args__ = (
        Index("ix_billing_provider_switch_attempts_creator_id", "creator_id"),
        UniqueConstraint(
            "creator_id",
            name="uq_billing_provider_switch_attempts_creator_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    creator_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("creators.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_billing_provider: Mapped[str] = mapped_column(String(32), nullable=False)
    target_billing_provider: Mapped[str] = mapped_column(String(32), nullable=False)
    target_billing_connect_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=DEFAULT_BILLING_CONNECT_STATUS,
        server_default=DEFAULT_BILLING_CONNECT_STATUS,
    )
    target_billing_account_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    target_billing_connected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    target_billing_provider_correlation_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
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

    creator = relationship("Creator", back_populates="billing_provider_switch_attempt")

