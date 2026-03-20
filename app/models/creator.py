from datetime import datetime
import uuid

from sqlalchemy import DateTime, String, event, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.billing_provider import (
    BILLING_PROVIDER_STRIPE,
    DEFAULT_BILLING_CONNECT_STATUS,
    DEFAULT_BILLING_PROVIDER,
)


class Creator(Base):
    __tablename__ = "creators"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # PP-1 keeps one active billing-provider slot per creator in V1.
    billing_provider: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=DEFAULT_BILLING_PROVIDER,
        server_default=DEFAULT_BILLING_PROVIDER,
    )
    billing_connect_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=DEFAULT_BILLING_CONNECT_STATUS,
        server_default=DEFAULT_BILLING_CONNECT_STATUS,
    )
    billing_connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    billing_account_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    billing_provider_correlation_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
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
    blocked_billing_cases = relationship(
        "BlockedBillingCase",
        back_populates="creator",
    )
    invoice_payment_events = relationship("InvoicePaymentEvent", back_populates="creator")
    content_items = relationship(
        "Content",
        back_populates="creator",
        cascade="all, delete-orphan",
    )
    content_fetch_snapshots = relationship(
        "ContentFetchSnapshot",
        back_populates="creator",
        cascade="all, delete-orphan",
    )
    content_extraction_artifacts = relationship(
        "ContentExtractionArtifact",
        back_populates="creator",
        cascade="all, delete-orphan",
    )
    content_confirmed_topics = relationship(
        "ContentConfirmedTopic",
        back_populates="creator",
        cascade="all, delete-orphan",
    )
    content_topic_candidates = relationship(
        "ContentTopicCandidate",
        back_populates="creator",
        cascade="all, delete-orphan",
    )
    support_requests = relationship(
        "SupportRequestRecord",
        back_populates="creator",
        cascade="all, delete-orphan",
    )

    @property
    def resolved_billing_provider(self) -> str:
        return self.billing_provider or DEFAULT_BILLING_PROVIDER

    @property
    def resolved_billing_connect_status(self) -> str:
        if self.billing_connect_status:
            return self.billing_connect_status
        if self.resolved_billing_provider == BILLING_PROVIDER_STRIPE:
            return self.stripe_connect_status or DEFAULT_BILLING_CONNECT_STATUS
        return DEFAULT_BILLING_CONNECT_STATUS

    @property
    def resolved_billing_account_id(self) -> str | None:
        if self.billing_account_id is not None:
            return self.billing_account_id
        if self.resolved_billing_provider == BILLING_PROVIDER_STRIPE:
            return self.stripe_account_id
        return None

    @property
    def resolved_billing_connected_at(self) -> datetime | None:
        if self.billing_connected_at is not None:
            return self.billing_connected_at
        if self.resolved_billing_provider == BILLING_PROVIDER_STRIPE:
            return self.stripe_connected_at
        return None


def _sync_creator_billing_identity(target: Creator) -> None:
    if not target.billing_provider:
        target.billing_provider = DEFAULT_BILLING_PROVIDER

    if not target.billing_connect_status:
        if target.billing_provider == BILLING_PROVIDER_STRIPE and target.stripe_connect_status:
            target.billing_connect_status = target.stripe_connect_status
        else:
            target.billing_connect_status = DEFAULT_BILLING_CONNECT_STATUS

    if target.billing_provider != BILLING_PROVIDER_STRIPE:
        return

    if target.billing_account_id is None and target.stripe_account_id is not None:
        target.billing_account_id = target.stripe_account_id

    if target.billing_connected_at is None and target.stripe_connected_at is not None:
        target.billing_connected_at = target.stripe_connected_at


@event.listens_for(Creator, "before_insert")
def _creator_before_insert(_mapper, _connection, target: Creator) -> None:
    _sync_creator_billing_identity(target)


@event.listens_for(Creator, "before_update")
def _creator_before_update(_mapper, _connection, target: Creator) -> None:
    _sync_creator_billing_identity(target)
