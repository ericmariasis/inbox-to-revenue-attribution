from datetime import datetime
import uuid

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class ContentFetchSnapshot(Base):
    __tablename__ = "content_fetch_snapshots"
    __table_args__ = (
        Index("ix_content_fetch_snapshots_content_id", "content_id"),
        Index("ix_content_fetch_snapshots_creator_id", "creator_id"),
        Index("ix_content_fetch_snapshots_fetch_status", "fetch_status"),
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
    requested_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    fetched_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    fetch_status: Mapped[str] = mapped_column(String(32), nullable=False)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    failure_reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_content_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    response_content_charset: Mapped[str | None] = mapped_column(String(64), nullable=True)
    snapshot_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    creator = relationship("Creator", back_populates="content_fetch_snapshots")
    content = relationship("Content", back_populates="fetch_snapshots")
    extraction_artifact = relationship(
        "ContentExtractionArtifact",
        back_populates="fetch_snapshot",
        uselist=False,
        cascade="all, delete-orphan",
    )
