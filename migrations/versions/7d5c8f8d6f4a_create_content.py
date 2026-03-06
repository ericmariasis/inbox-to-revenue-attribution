"""create content

Revision ID: 7d5c8f8d6f4a
Revises: c6f6a5b1d3e2
Create Date: 2026-03-06 13:45:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "7d5c8f8d6f4a"
down_revision: Union[str, Sequence[str], None] = "c6f6a5b1d3e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "content",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("creator_id", sa.UUID(), nullable=False),
        sa.Column("booking_link_id", sa.UUID(), nullable=False),
        sa.Column("source_url", sa.String(length=2048), nullable=False),
        sa.Column("tid", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["booking_link_id"], ["booking_links.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["creator_id"], ["creators.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tid", name="uq_content_tid"),
    )
    op.create_index("ix_content_booking_link_id", "content", ["booking_link_id"], unique=False)
    op.create_index("ix_content_creator_id", "content", ["creator_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_content_creator_id", table_name="content")
    op.drop_index("ix_content_booking_link_id", table_name="content")
    op.drop_table("content")
