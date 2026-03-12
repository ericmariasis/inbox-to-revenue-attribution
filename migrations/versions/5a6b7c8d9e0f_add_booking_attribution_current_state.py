"""add booking attribution current state

Revision ID: 5a6b7c8d9e0f
Revises: 4f5a6b7c8d9e
Create Date: 2026-03-12 13:20:00.000000
"""

from collections.abc import Sequence
from typing import Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "5a6b7c8d9e0f"
down_revision: Union[str, None] = "4f5a6b7c8d9e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "bookings",
        sa.Column(
            "attribution_status",
            sa.String(length=32),
            nullable=False,
            server_default="attributed",
        ),
    )
    op.add_column(
        "bookings",
        sa.Column("unattributed_reason", sa.String(length=64), nullable=True),
    )
    op.alter_column(
        "bookings",
        "tid",
        existing_type=sa.String(length=64),
        nullable=True,
    )
    op.create_index(
        "ix_bookings_attribution_status",
        "bookings",
        ["attribution_status"],
        unique=False,
    )
    op.create_check_constraint(
        "ck_bookings_attribution_current_state",
        "bookings",
        "("
        "(attribution_status = 'attributed' AND tid IS NOT NULL AND unattributed_reason IS NULL)"
        " OR "
        "(attribution_status = 'unattributed' AND tid IS NULL AND unattributed_reason IS NOT NULL)"
        ")",
    )


def downgrade() -> None:
    op.drop_constraint("ck_bookings_attribution_current_state", "bookings", type_="check")
    op.drop_index("ix_bookings_attribution_status", table_name="bookings")
    op.alter_column(
        "bookings",
        "tid",
        existing_type=sa.String(length=64),
        nullable=False,
    )
    op.drop_column("bookings", "unattributed_reason")
    op.drop_column("bookings", "attribution_status")
