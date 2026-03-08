"""create invoice payment events

Revision ID: 8a5b6c7d9e0f
Revises: c4a7f9e2d1b6
Create Date: 2026-03-08 12:15:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "8a5b6c7d9e0f"
down_revision: Union[str, Sequence[str], None] = "c4a7f9e2d1b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "invoice_payment_events",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("stripe_event_id", sa.String(length=255), nullable=False),
        sa.Column("stripe_event_type", sa.String(length=128), nullable=False),
        sa.Column("stripe_account_id", sa.String(length=255), nullable=True),
        sa.Column("stripe_invoice_id", sa.String(length=255), nullable=False),
        sa.Column("invoice_id", sa.UUID(), nullable=True),
        sa.Column("creator_id", sa.UUID(), nullable=True),
        sa.Column("booking_id", sa.UUID(), nullable=True),
        sa.Column("tid", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("unattributed_reason", sa.String(length=64), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["invoice_id"], ["invoices.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["creator_id"], ["creators.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["booking_id"], ["bookings.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tid"], ["content.tid"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("stripe_event_id", name="uq_invoice_payment_events_stripe_event_id"),
    )
    op.create_index(
        "ix_invoice_payment_events_invoice_id",
        "invoice_payment_events",
        ["invoice_id"],
        unique=False,
    )
    op.create_index(
        "ix_invoice_payment_events_creator_id",
        "invoice_payment_events",
        ["creator_id"],
        unique=False,
    )
    op.create_index(
        "ix_invoice_payment_events_booking_id",
        "invoice_payment_events",
        ["booking_id"],
        unique=False,
    )
    op.create_index(
        "ix_invoice_payment_events_tid",
        "invoice_payment_events",
        ["tid"],
        unique=False,
    )
    op.create_index(
        "ix_invoice_payment_events_stripe_invoice_id",
        "invoice_payment_events",
        ["stripe_invoice_id"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_invoice_payment_events_stripe_invoice_id", table_name="invoice_payment_events")
    op.drop_index("ix_invoice_payment_events_tid", table_name="invoice_payment_events")
    op.drop_index("ix_invoice_payment_events_booking_id", table_name="invoice_payment_events")
    op.drop_index("ix_invoice_payment_events_creator_id", table_name="invoice_payment_events")
    op.drop_index("ix_invoice_payment_events_invoice_id", table_name="invoice_payment_events")
    op.drop_table("invoice_payment_events")
