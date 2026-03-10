from datetime import datetime
import uuid

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class ContentExtractionArtifact(Base):
    __tablename__ = "content_extraction_artifacts"
    __table_args__ = (
        Index("ix_content_extraction_artifacts_content_id", "content_id"),
        Index("ix_content_extraction_artifacts_creator_id", "creator_id"),
        Index("ix_content_extraction_artifacts_extraction_status", "extraction_status"),
        UniqueConstraint("fetch_snapshot_id", name="uq_content_extraction_artifacts_fetch_snapshot_id"),
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
    fetch_snapshot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("content_fetch_snapshots.id", ondelete="CASCADE"),
        nullable=False,
    )
    extraction_status: Mapped[str] = mapped_column(String(32), nullable=False)
    extraction_reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    extraction_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    extraction_method: Mapped[str | None] = mapped_column(String(64), nullable=True)
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_at_raw: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_text_char_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    extracted_text_char_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    extracted_text_word_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    extracted_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    creator = relationship("Creator", back_populates="content_extraction_artifacts")
    content = relationship("Content", back_populates="extraction_artifacts")
    fetch_snapshot = relationship("ContentFetchSnapshot", back_populates="extraction_artifact")
