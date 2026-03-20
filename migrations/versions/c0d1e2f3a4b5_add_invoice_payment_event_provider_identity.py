"""add invoice payment event provider identity

Revision ID: c0d1e2f3a4b5
Revises: 9a0b1c2d3e4f
Create Date: 2026-03-20 14:05:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c0d1e2f3a4b5"
down_revision: Union[str, None] = "9a0b1c2d3e4f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "invoice_payment_events",
        sa.Column(
            "payment_provider",
            sa.String(length=32),
            nullable=False,
            server_default="stripe",
        ),
    )
    op.add_column(
        "invoice_payment_events",
        sa.Column("provider_event_id", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "invoice_payment_events",
        sa.Column("provider_event_type", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "invoice_payment_events",
        sa.Column("provider_account_id", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "invoice_payment_events",
        sa.Column("provider_invoice_id", sa.String(length=255), nullable=True),
    )
    op.execute(
        """
        UPDATE invoice_payment_events
        SET payment_provider = 'stripe',
            provider_event_id = stripe_event_id,
            provider_event_type = stripe_event_type,
            provider_account_id = stripe_account_id,
            provider_invoice_id = stripe_invoice_id
        """
    )
    op.alter_column(
        "invoice_payment_events",
        "stripe_event_id",
        existing_type=sa.String(length=255),
        nullable=True,
    )
    op.alter_column(
        "invoice_payment_events",
        "stripe_event_type",
        existing_type=sa.String(length=128),
        nullable=True,
    )
    op.alter_column(
        "invoice_payment_events",
        "stripe_invoice_id",
        existing_type=sa.String(length=255),
        nullable=True,
    )
    op.create_unique_constraint(
        "uq_invoice_payment_events_provider_event_identity",
        "invoice_payment_events",
        ["payment_provider", "provider_event_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_invoice_payment_events_provider_event_identity",
        "invoice_payment_events",
        type_="unique",
    )
    op.alter_column(
        "invoice_payment_events",
        "stripe_invoice_id",
        existing_type=sa.String(length=255),
        nullable=False,
    )
    op.alter_column(
        "invoice_payment_events",
        "stripe_event_type",
        existing_type=sa.String(length=128),
        nullable=False,
    )
    op.alter_column(
        "invoice_payment_events",
        "stripe_event_id",
        existing_type=sa.String(length=255),
        nullable=False,
    )
    op.drop_column("invoice_payment_events", "provider_invoice_id")
    op.drop_column("invoice_payment_events", "provider_account_id")
    op.drop_column("invoice_payment_events", "provider_event_type")
    op.drop_column("invoice_payment_events", "provider_event_id")
    op.drop_column("invoice_payment_events", "payment_provider")
