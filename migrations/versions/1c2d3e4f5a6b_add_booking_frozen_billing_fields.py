"""add booking frozen billing fields

Revision ID: 1c2d3e4f5a6b
Revises: f7a8b9c0d1e2
Create Date: 2026-03-11 13:30:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "1c2d3e4f5a6b"
down_revision: Union[str, None] = "f7a8b9c0d1e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("bookings", sa.Column("frozen_billing_amount_cents", sa.Integer(), nullable=True))
    op.add_column("bookings", sa.Column("frozen_billing_currency", sa.String(length=3), nullable=True))

    op.execute(
        """
        UPDATE bookings
        SET
            frozen_billing_amount_cents = invoices.amount_cents,
            frozen_billing_currency = invoices.currency
        FROM invoices
        WHERE invoices.booking_id = bookings.id
          AND bookings.frozen_billing_amount_cents IS NULL
          AND bookings.frozen_billing_currency IS NULL
        """
    )
    op.execute(
        """
        UPDATE bookings
        SET
            frozen_billing_amount_cents = blocked_billing_cases.frozen_amount_cents,
            frozen_billing_currency = blocked_billing_cases.frozen_currency
        FROM blocked_billing_cases
        WHERE blocked_billing_cases.booking_id = bookings.id
          AND bookings.frozen_billing_amount_cents IS NULL
          AND bookings.frozen_billing_currency IS NULL
        """
    )


def downgrade() -> None:
    op.drop_column("bookings", "frozen_billing_currency")
    op.drop_column("bookings", "frozen_billing_amount_cents")
