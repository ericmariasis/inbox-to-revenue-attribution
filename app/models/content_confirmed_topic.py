from datetime import datetime
import uuid

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class ContentConfirmedTopic(Base):
    __tablename__ = "content_confirmed_topics"
    __table_args__ = (
        Index("ix_content_confirmed_topics_content_id", "content_id"),
        Index("ix_content_confirmed_topics_creator_id", "creator_id"),
        UniqueConstraint(
            "content_id",
            "normalized_label",
            name="uq_content_confirmed_topics_content_id_normalized_label",
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
    canonical_label: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_label: Mapped[str] = mapped_column(String(255), nullable=False)
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

    creator = relationship("Creator", back_populates="content_confirmed_topics")
    content = relationship("Content", back_populates="confirmed_topics")
    topic_candidates = relationship(
        "ContentTopicCandidate",
        back_populates="confirmed_topic",
    )
