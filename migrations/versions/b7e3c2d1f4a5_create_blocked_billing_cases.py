"""create blocked billing cases

Revision ID: b7e3c2d1f4a5
Revises: 8a5b6c7d9e0f
Create Date: 2026-03-09 16:45:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "b7e3c2d1f4a5"
down_revision: Union[str, None] = "8a5b6c7d9e0f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "blocked_billing_cases",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("creator_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("booking_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("invoice_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("tid", sa.String(length=64), nullable=False),
        sa.Column("calendly_booking_uuid", sa.String(length=255), nullable=False),
        sa.Column("stripe_account_id", sa.String(length=255), nullable=True),
        sa.Column("frozen_amount_cents", sa.Integer(), nullable=False),
        sa.Column("frozen_currency", sa.String(length=3), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="open", nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column("provider_operation", sa.String(length=64), nullable=True),
        sa.Column("provider_http_status", sa.Integer(), nullable=True),
        sa.Column("provider_error_code", sa.String(length=64), nullable=True),
        sa.Column("first_blocked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_blocked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution_code", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(["booking_id"], ["bookings.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["creator_id"], ["creators.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["invoice_id"], ["invoices.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("booking_id", name="uq_blocked_billing_cases_booking_id"),
    )
    op.create_index(
        "ix_blocked_billing_cases_creator_id",
        "blocked_billing_cases",
        ["creator_id"],
        unique=False,
    )
    op.create_index(
        "ix_blocked_billing_cases_status",
        "blocked_billing_cases",
        ["status"],
        unique=False,
    )
    op.create_index(
        "ix_blocked_billing_cases_tid",
        "blocked_billing_cases",
        ["tid"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_blocked_billing_cases_tid", table_name="blocked_billing_cases")
    op.drop_index("ix_blocked_billing_cases_status", table_name="blocked_billing_cases")
    op.drop_index("ix_blocked_billing_cases_creator_id", table_name="blocked_billing_cases")
    op.drop_table("blocked_billing_cases")
