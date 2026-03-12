from datetime import datetime
import uuid

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class CreatorClaimSnapshotRecord(Base):
    __tablename__ = "creator_claim_snapshots"
    __table_args__ = (
        Index("ix_creator_claim_snapshots_creator_id", "creator_id"),
        Index("ix_creator_claim_snapshots_content_id", "content_id"),
        Index("ix_creator_claim_snapshots_claim_kind", "claim_kind"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    creator_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("creators.id", ondelete="CASCADE"),
        nullable=False,
    )
    content_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("content.id", ondelete="CASCADE"),
        nullable=False,
    )
    authoritative_extraction_artifact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("content_extraction_artifacts.id", ondelete="CASCADE"),
        nullable=False,
    )
    authoritative_fetch_snapshot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("content_fetch_snapshots.id", ondelete="CASCADE"),
        nullable=False,
    )
    claim_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    claim_contract_version: Mapped[str] = mapped_column(String(128), nullable=False)
    claim_reducer_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    claim_prompt_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    rendered_claim_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    creator = relationship("Creator")
    content = relationship("Content")
    authoritative_extraction_artifact = relationship(
        "ContentExtractionArtifact",
        foreign_keys=[authoritative_extraction_artifact_id],
    )
    authoritative_fetch_snapshot = relationship(
        "ContentFetchSnapshot",
        foreign_keys=[authoritative_fetch_snapshot_id],
    )
    paid_evidence_references = relationship(
        "CreatorClaimPaidEvidenceReference",
        back_populates="claim_snapshot",
        cascade="all, delete-orphan",
        order_by="CreatorClaimPaidEvidenceReference.evidence_order",
    )
