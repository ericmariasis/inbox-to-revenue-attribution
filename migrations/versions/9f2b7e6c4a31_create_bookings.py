"""create bookings

Revision ID: 9f2b7e6c4a31
Revises: 7d5c8f8d6f4a
Create Date: 2026-03-07 10:30:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "9f2b7e6c4a31"
down_revision: Union[str, Sequence[str], None] = "7d5c8f8d6f4a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "bookings",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("creator_id", sa.UUID(), nullable=False),
        sa.Column("tid", sa.String(length=64), nullable=False),
        sa.Column("booking_link_id", sa.UUID(), nullable=False),
        sa.Column("calendly_booking_uuid", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="created", nullable=False),
        sa.Column("booked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("canceled_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["booking_link_id"], ["booking_links.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["creator_id"], ["creators.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tid"], ["content.tid"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("calendly_booking_uuid", name="uq_bookings_calendly_booking_uuid"),
    )
    op.create_index("ix_bookings_booking_link_id", "bookings", ["booking_link_id"], unique=False)
    op.create_index("ix_bookings_creator_id", "bookings", ["creator_id"], unique=False)
    op.create_index("ix_bookings_tid", "bookings", ["tid"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_bookings_tid", table_name="bookings")
    op.drop_index("ix_bookings_creator_id", table_name="bookings")
    op.drop_index("ix_bookings_booking_link_id", table_name="bookings")
    op.drop_table("bookings")
