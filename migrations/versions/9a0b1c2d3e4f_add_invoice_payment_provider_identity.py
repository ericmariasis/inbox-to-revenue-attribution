"""add invoice payment provider identity

Revision ID: 9a0b1c2d3e4f
Revises: 8f9a0b1c2d3e
Create Date: 2026-03-20 12:15:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "9a0b1c2d3e4f"
down_revision: Union[str, None] = "8f9a0b1c2d3e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "invoices",
        sa.Column(
            "payment_provider",
            sa.String(length=32),
            nullable=False,
            server_default="stripe",
        ),
    )
    op.add_column(
        "invoices",
        sa.Column("provider_account_id", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "invoices",
        sa.Column("provider_invoice_id", sa.String(length=255), nullable=True),
    )
    op.execute(
        """
        UPDATE invoices
        SET payment_provider = 'stripe',
            provider_account_id = stripe_account_id,
            provider_invoice_id = stripe_invoice_id
        """
    )
    op.alter_column(
        "invoices",
        "stripe_account_id",
        existing_type=sa.String(length=255),
        nullable=True,
    )
    op.alter_column(
        "invoices",
        "stripe_invoice_id",
        existing_type=sa.String(length=255),
        nullable=True,
    )
    op.create_unique_constraint(
        "uq_invoices_provider_invoice_identity",
        "invoices",
        ["payment_provider", "provider_account_id", "provider_invoice_id"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_invoices_provider_invoice_identity", "invoices", type_="unique")
    op.alter_column(
        "invoices",
        "stripe_invoice_id",
        existing_type=sa.String(length=255),
        nullable=False,
    )
    op.alter_column(
        "invoices",
        "stripe_account_id",
        existing_type=sa.String(length=255),
        nullable=False,
    )
    op.drop_column("invoices", "provider_invoice_id")
    op.drop_column("invoices", "provider_account_id")
    op.drop_column("invoices", "payment_provider")
