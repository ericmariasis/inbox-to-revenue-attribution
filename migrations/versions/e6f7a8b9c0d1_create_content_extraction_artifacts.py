"""create content extraction artifacts

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-03-10 12:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "e6f7a8b9c0d1"
down_revision: Union[str, None] = "d5e6f7a8b9c0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "content_extraction_artifacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("content_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("creator_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("fetch_snapshot_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("extraction_status", sa.String(length=32), nullable=False),
        sa.Column("extraction_reason_code", sa.String(length=64), nullable=True),
        sa.Column("extraction_detail", sa.Text(), nullable=True),
        sa.Column("extraction_method", sa.String(length=64), nullable=True),
        sa.Column("title", sa.String(length=512), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at_raw", sa.String(length=255), nullable=True),
        sa.Column("source_text_char_count", sa.Integer(), nullable=True),
        sa.Column("extracted_text_char_count", sa.Integer(), nullable=True),
        sa.Column("extracted_text_word_count", sa.Integer(), nullable=True),
        sa.Column("extracted_text", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["content_id"], ["content.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["creator_id"], ["creators.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["fetch_snapshot_id"], ["content_fetch_snapshots.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "fetch_snapshot_id",
            name="uq_content_extraction_artifacts_fetch_snapshot_id",
        ),
    )
    op.create_index(
        "ix_content_extraction_artifacts_content_id",
        "content_extraction_artifacts",
        ["content_id"],
        unique=False,
    )
    op.create_index(
        "ix_content_extraction_artifacts_creator_id",
        "content_extraction_artifacts",
        ["creator_id"],
        unique=False,
    )
    op.create_index(
        "ix_content_extraction_artifacts_extraction_status",
        "content_extraction_artifacts",
        ["extraction_status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_content_extraction_artifacts_extraction_status",
        table_name="content_extraction_artifacts",
    )
    op.drop_index("ix_content_extraction_artifacts_creator_id", table_name="content_extraction_artifacts")
    op.drop_index("ix_content_extraction_artifacts_content_id", table_name="content_extraction_artifacts")
    op.drop_table("content_extraction_artifacts")
