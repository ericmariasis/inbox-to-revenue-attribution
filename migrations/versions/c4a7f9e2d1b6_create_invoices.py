"""create invoices

Revision ID: c4a7f9e2d1b6
Revises: a3d2f4b8c9e1
Create Date: 2026-03-08 09:30:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c4a7f9e2d1b6"
down_revision: Union[str, Sequence[str], None] = "a3d2f4b8c9e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "invoices",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("creator_id", sa.UUID(), nullable=False),
        sa.Column("booking_id", sa.UUID(), nullable=False),
        sa.Column("tid", sa.String(length=64), nullable=False),
        sa.Column("stripe_account_id", sa.String(length=255), nullable=False),
        sa.Column("stripe_invoice_id", sa.String(length=255), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="open", nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("voided_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["booking_id"], ["bookings.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["creator_id"], ["creators.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tid"], ["content.tid"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("booking_id", name="uq_invoices_booking_id"),
        sa.UniqueConstraint("stripe_invoice_id", name="uq_invoices_stripe_invoice_id"),
    )
    op.create_index("ix_invoices_creator_id", "invoices", ["creator_id"], unique=False)
    op.create_index("ix_invoices_tid", "invoices", ["tid"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_invoices_tid", table_name="invoices")
    op.drop_index("ix_invoices_creator_id", table_name="invoices")
    op.drop_table("invoices")
