"""create content fetch snapshots

Revision ID: d5e6f7a8b9c0
Revises: b7e3c2d1f4a5
Create Date: 2026-03-10 10:15:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "d5e6f7a8b9c0"
down_revision: Union[str, None] = "b7e3c2d1f4a5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "content_fetch_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("content_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("creator_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("requested_url", sa.String(length=2048), nullable=False),
        sa.Column("fetched_url", sa.String(length=2048), nullable=True),
        sa.Column("fetch_status", sa.String(length=32), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("failure_reason_code", sa.String(length=64), nullable=True),
        sa.Column("failure_detail", sa.Text(), nullable=True),
        sa.Column("response_content_type", sa.String(length=255), nullable=True),
        sa.Column("response_content_charset", sa.String(length=64), nullable=True),
        sa.Column("snapshot_text", sa.Text(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["content_id"], ["content.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["creator_id"], ["creators.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_content_fetch_snapshots_content_id",
        "content_fetch_snapshots",
        ["content_id"],
        unique=False,
    )
    op.create_index(
        "ix_content_fetch_snapshots_creator_id",
        "content_fetch_snapshots",
        ["creator_id"],
        unique=False,
    )
    op.create_index(
        "ix_content_fetch_snapshots_fetch_status",
        "content_fetch_snapshots",
        ["fetch_status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_content_fetch_snapshots_fetch_status", table_name="content_fetch_snapshots")
    op.drop_index("ix_content_fetch_snapshots_creator_id", table_name="content_fetch_snapshots")
    op.drop_index("ix_content_fetch_snapshots_content_id", table_name="content_fetch_snapshots")
    op.drop_table("content_fetch_snapshots")
