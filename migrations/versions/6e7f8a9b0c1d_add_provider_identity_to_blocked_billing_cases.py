"""add provider identity to blocked billing cases

Revision ID: 6e7f8a9b0c1d
Revises: 5e6f7a8b9c0d
Create Date: 2026-03-18 18:45:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "6e7f8a9b0c1d"
down_revision: Union[str, None] = "5e6f7a8b9c0d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "blocked_billing_cases",
        sa.Column(
            "provider",
            sa.String(length=32),
            nullable=False,
            server_default="calendly",
        ),
    )
    op.add_column(
        "blocked_billing_cases",
        sa.Column("provider_booking_id", sa.String(length=255), nullable=True),
    )
    op.execute(
        """
        UPDATE blocked_billing_cases
        SET provider_booking_id = calendly_booking_uuid
        WHERE provider_booking_id IS NULL
        """
    )
    op.alter_column(
        "blocked_billing_cases",
        "provider_booking_id",
        existing_type=sa.String(length=255),
        nullable=False,
    )
    op.alter_column(
        "blocked_billing_cases",
        "calendly_booking_uuid",
        existing_type=sa.String(length=255),
        nullable=True,
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE blocked_billing_cases
        SET calendly_booking_uuid = COALESCE(calendly_booking_uuid, provider_booking_id)
        """
    )
    op.alter_column(
        "blocked_billing_cases",
        "calendly_booking_uuid",
        existing_type=sa.String(length=255),
        nullable=False,
    )
    op.drop_column("blocked_billing_cases", "provider_booking_id")
    op.drop_column("blocked_billing_cases", "provider")
