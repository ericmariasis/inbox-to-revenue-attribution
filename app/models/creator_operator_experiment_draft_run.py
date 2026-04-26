from datetime import datetime
import uuid

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class CreatorOperatorExperimentDraftRunRecord(Base):
    __tablename__ = "creator_operator_experiment_draft_runs"
    __table_args__ = (
        Index("ix_creator_operator_experiment_draft_runs_creator_id", "creator_id"),
        Index("ix_creator_operator_experiment_draft_runs_status", "status"),
        Index("ix_creator_operator_experiment_draft_runs_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    creator_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("creators.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    summary_text: Mapped[str] = mapped_column(Text, nullable=False)
    run_generator_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    run_model_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    run_config_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    run_contract_version: Mapped[str] = mapped_column(String(128), nullable=False)
    run_reducer_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    run_prompt_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    creator = relationship("Creator")
    cards = relationship(
        "CreatorOperatorExperimentDraftRunCardRecord",
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="CreatorOperatorExperimentDraftRunCardRecord.card_order",
    )
