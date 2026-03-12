import uuid

from sqlalchemy import ForeignKey, Index, Integer, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class CreatorClaimPaidEvidenceReference(Base):
    __tablename__ = "creator_claim_paid_evidence_refs"
    __table_args__ = (
        Index("ix_creator_claim_paid_refs_snapshot_id", "claim_snapshot_id"),
        Index("ix_creator_claim_paid_refs_invoice_id", "invoice_id"),
        UniqueConstraint(
            "claim_snapshot_id",
            "evidence_order",
            name="uq_creator_claim_paid_refs_snapshot_order",
        ),
        UniqueConstraint(
            "claim_snapshot_id",
            "invoice_id",
            name="uq_creator_claim_paid_refs_snapshot_invoice",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    claim_snapshot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("creator_claim_snapshots.id", ondelete="CASCADE"),
        nullable=False,
    )
    booking_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("bookings.id", ondelete="CASCADE"),
        nullable=False,
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("invoices.id", ondelete="CASCADE"),
        nullable=False,
    )
    payment_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("invoice_payment_events.id", ondelete="SET NULL"),
        nullable=True,
    )
    evidence_order: Mapped[int] = mapped_column(Integer, nullable=False)

    claim_snapshot = relationship(
        "CreatorClaimSnapshotRecord",
        back_populates="paid_evidence_references",
    )
    booking = relationship("Booking")
    invoice = relationship("Invoice")
    payment_event = relationship("InvoicePaymentEvent")
