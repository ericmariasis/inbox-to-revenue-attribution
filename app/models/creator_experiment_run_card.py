import uuid

from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class CreatorExperimentRunCardRecord(Base):
    __tablename__ = "creator_experiment_run_cards"
    __table_args__ = (
        Index("ix_creator_experiment_run_cards_run_id", "run_id"),
        Index("ix_creator_experiment_run_cards_claim_snapshot_id", "claim_snapshot_id"),
        UniqueConstraint(
            "run_id",
            "card_order",
            name="uq_creator_experiment_run_cards_run_order",
        ),
        UniqueConstraint(
            "run_id",
            "claim_snapshot_id",
            name="uq_creator_experiment_run_cards_run_claim_snapshot",
        ),
        UniqueConstraint(
            "run_id",
            "card_id",
            name="uq_creator_experiment_run_cards_run_card_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("creator_experiment_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    claim_snapshot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("creator_claim_snapshots.id", ondelete="CASCADE"),
        nullable=False,
    )
    card_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    content_tid: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    hypothesis: Mapped[str] = mapped_column(Text, nullable=False)
    why_this_might_work: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_summary: Mapped[str] = mapped_column(Text, nullable=False)
    caution: Mapped[str] = mapped_column(Text, nullable=False)
    card_order: Mapped[int] = mapped_column(Integer, nullable=False)

    run = relationship(
        "CreatorExperimentRunRecord",
        back_populates="cards",
    )
    claim_snapshot = relationship("CreatorClaimSnapshotRecord")
