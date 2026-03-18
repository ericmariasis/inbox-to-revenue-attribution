"""add provider aware booking fields

Revision ID: 4d5e6f7a8b9c
Revises: 3c4d5e6f7a8b
Create Date: 2026-03-17 21:30:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "4d5e6f7a8b9c"
down_revision: Union[str, None] = "3c4d5e6f7a8b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "booking_links",
        sa.Column(
            "provider",
            sa.String(length=32),
            nullable=False,
            server_default="calendly",
        ),
    )
    op.add_column(
        "booking_links",
        sa.Column("destination_url", sa.String(length=2048), nullable=True),
    )
    op.execute(
        """
        UPDATE booking_links
        SET destination_url = calendly_url
        WHERE destination_url IS NULL
        """
    )
    op.alter_column(
        "booking_links",
        "calendly_url",
        existing_type=sa.String(length=2048),
        nullable=True,
    )

    op.add_column(
        "bookings",
        sa.Column(
            "provider",
            sa.String(length=32),
            nullable=False,
            server_default="calendly",
        ),
    )
    op.add_column(
        "bookings",
        sa.Column("provider_booking_id", sa.String(length=255), nullable=True),
    )
    op.execute(
        """
        UPDATE bookings
        SET provider_booking_id = calendly_booking_uuid
        WHERE provider_booking_id IS NULL
        """
    )
    op.alter_column(
        "bookings",
        "calendly_booking_uuid",
        existing_type=sa.String(length=255),
        nullable=True,
    )
    op.create_unique_constraint(
        "uq_bookings_provider_provider_booking_id",
        "bookings",
        ["provider", "provider_booking_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_bookings_provider_provider_booking_id",
        "bookings",
        type_="unique",
    )
    op.execute(
        """
        UPDATE bookings
        SET calendly_booking_uuid = COALESCE(calendly_booking_uuid, provider_booking_id)
        """
    )
    op.alter_column(
        "bookings",
        "calendly_booking_uuid",
        existing_type=sa.String(length=255),
        nullable=False,
    )
    op.drop_column("bookings", "provider_booking_id")
    op.drop_column("bookings", "provider")

    op.execute(
        """
        UPDATE booking_links
        SET calendly_url = COALESCE(calendly_url, destination_url)
        """
    )
    op.alter_column(
        "booking_links",
        "calendly_url",
        existing_type=sa.String(length=2048),
        nullable=False,
    )
    op.drop_column("booking_links", "destination_url")
    op.drop_column("booking_links", "provider")
