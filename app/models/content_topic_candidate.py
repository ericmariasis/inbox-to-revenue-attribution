from datetime import datetime
import uuid

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class ContentTopicCandidate(Base):
    __tablename__ = "content_topic_candidates"
    __table_args__ = (
        Index("ix_content_topic_candidates_content_id", "content_id"),
        Index("ix_content_topic_candidates_creator_id", "creator_id"),
        Index("ix_content_topic_candidates_extraction_artifact_id", "extraction_artifact_id"),
        Index("ix_content_topic_candidates_review_status", "review_status"),
        UniqueConstraint(
            "extraction_artifact_id",
            "normalized_label",
            name="uq_content_topic_candidates_artifact_id_normalized_label",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    content_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("content.id", ondelete="CASCADE"),
        nullable=False,
    )
    creator_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("creators.id", ondelete="CASCADE"),
        nullable=False,
    )
    extraction_artifact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("content_extraction_artifacts.id", ondelete="CASCADE"),
        nullable=False,
    )
    confirmed_topic_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("content_confirmed_topics.id", ondelete="SET NULL"),
        nullable=True,
    )
    suggested_label: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_label: Mapped[str] = mapped_column(String(255), nullable=False)
    suggestion_method: Mapped[str] = mapped_column(String(64), nullable=False)
    candidate_rank: Mapped[int] = mapped_column(Integer, nullable=False)
    review_status: Mapped[str] = mapped_column(String(32), nullable=False)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    creator = relationship("Creator", back_populates="content_topic_candidates")
    content = relationship("Content", back_populates="topic_candidates")
    extraction_artifact = relationship("ContentExtractionArtifact", back_populates="topic_candidates")
    confirmed_topic = relationship("ContentConfirmedTopic", back_populates="topic_candidates")
