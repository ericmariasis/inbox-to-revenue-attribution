"""add provider neutral billing snapshot to blocked billing cases

Revision ID: c7d8e9f0a1b2
Revises: b4c5d6e7f8a9
Create Date: 2026-03-23 13:15:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "c7d8e9f0a1b2"
down_revision: Union[str, None] = "b4c5d6e7f8a9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "blocked_billing_cases",
        sa.Column("payment_provider", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "blocked_billing_cases",
        sa.Column("provider_account_id", sa.String(length=255), nullable=True),
    )
    op.execute(
        f"""
        UPDATE blocked_billing_cases
        SET
            payment_provider = COALESCE(payment_provider, 'stripe'),
            provider_account_id = COALESCE(provider_account_id, stripe_account_id)
        """
    )


def downgrade() -> None:
    op.drop_column("blocked_billing_cases", "provider_account_id")
    op.drop_column("blocked_billing_cases", "payment_provider")
