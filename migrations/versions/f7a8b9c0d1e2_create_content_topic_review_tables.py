"""create content topic review tables

Revision ID: f7a8b9c0d1e2
Revises: e6f7a8b9c0d1
Create Date: 2026-03-10 15:30:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "f7a8b9c0d1e2"
down_revision: Union[str, None] = "e6f7a8b9c0d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "content_confirmed_topics",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("content_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("creator_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("canonical_label", sa.String(length=255), nullable=False),
        sa.Column("normalized_label", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["content_id"], ["content.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["creator_id"], ["creators.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "content_id",
            "normalized_label",
            name="uq_content_confirmed_topics_content_id_normalized_label",
        ),
    )
    op.create_index(
        "ix_content_confirmed_topics_content_id",
        "content_confirmed_topics",
        ["content_id"],
        unique=False,
    )
    op.create_index(
        "ix_content_confirmed_topics_creator_id",
        "content_confirmed_topics",
        ["creator_id"],
        unique=False,
    )

    op.create_table(
        "content_topic_candidates",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("content_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("creator_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("extraction_artifact_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("confirmed_topic_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("suggested_label", sa.String(length=255), nullable=False),
        sa.Column("normalized_label", sa.String(length=255), nullable=False),
        sa.Column("suggestion_method", sa.String(length=64), nullable=False),
        sa.Column("candidate_rank", sa.Integer(), nullable=False),
        sa.Column("review_status", sa.String(length=32), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["content_id"], ["content.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["creator_id"], ["creators.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["extraction_artifact_id"],
            ["content_extraction_artifacts.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["confirmed_topic_id"],
            ["content_confirmed_topics.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "extraction_artifact_id",
            "normalized_label",
            name="uq_content_topic_candidates_artifact_id_normalized_label",
        ),
    )
    op.create_index(
        "ix_content_topic_candidates_content_id",
        "content_topic_candidates",
        ["content_id"],
        unique=False,
    )
    op.create_index(
        "ix_content_topic_candidates_creator_id",
        "content_topic_candidates",
        ["creator_id"],
        unique=False,
    )
    op.create_index(
        "ix_content_topic_candidates_extraction_artifact_id",
        "content_topic_candidates",
        ["extraction_artifact_id"],
        unique=False,
    )
    op.create_index(
        "ix_content_topic_candidates_review_status",
        "content_topic_candidates",
        ["review_status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_content_topic_candidates_review_status",
        table_name="content_topic_candidates",
    )
    op.drop_index(
        "ix_content_topic_candidates_extraction_artifact_id",
        table_name="content_topic_candidates",
    )
    op.drop_index(
        "ix_content_topic_candidates_creator_id",
        table_name="content_topic_candidates",
    )
    op.drop_index(
        "ix_content_topic_candidates_content_id",
        table_name="content_topic_candidates",
    )
    op.drop_table("content_topic_candidates")

    op.drop_index(
        "ix_content_confirmed_topics_creator_id",
        table_name="content_confirmed_topics",
    )
    op.drop_index(
        "ix_content_confirmed_topics_content_id",
        table_name="content_confirmed_topics",
    )
    op.drop_table("content_confirmed_topics")
