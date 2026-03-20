"""add creator billing provider identity

Revision ID: 8f9a0b1c2d3e
Revises: 6e7f8a9b0c1d
Create Date: 2026-03-20 10:30:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "8f9a0b1c2d3e"
down_revision: Union[str, None] = "6e7f8a9b0c1d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "creators",
        sa.Column(
            "billing_provider",
            sa.String(length=32),
            nullable=False,
            server_default="stripe",
        ),
    )
    op.add_column(
        "creators",
        sa.Column(
            "billing_connect_status",
            sa.String(length=32),
            nullable=False,
            server_default="pending",
        ),
    )
    op.add_column(
        "creators",
        sa.Column("billing_connected_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "creators",
        sa.Column("billing_account_id", sa.String(length=255), nullable=True),
    )
    op.execute(
        """
        UPDATE creators
        SET billing_provider = 'stripe',
            billing_connect_status = stripe_connect_status,
            billing_connected_at = stripe_connected_at,
            billing_account_id = stripe_account_id
        """
    )


def downgrade() -> None:
    op.drop_column("creators", "billing_account_id")
    op.drop_column("creators", "billing_connected_at")
    op.drop_column("creators", "billing_connect_status")
    op.drop_column("creators", "billing_provider")
