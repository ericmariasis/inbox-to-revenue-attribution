"""add calendly webhook reducer contract

Revision ID: 6b7c8d9e0f1a
Revises: 5a6b7c8d9e0f
Create Date: 2026-03-12 18:10:00.000000
"""

from collections.abc import Sequence
from typing import Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "6b7c8d9e0f1a"
down_revision: Union[str, None] = "5a6b7c8d9e0f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "calendly_webhook_events",
        sa.Column("reducer_key", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "calendly_webhook_events",
        sa.Column(
            "reducer_attempt_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.execute(
        "UPDATE calendly_webhook_events "
        "SET reducer_key = 'booking:' || calendly_booking_uuid"
    )
    op.execute(
        "UPDATE calendly_webhook_events "
        "SET reducer_attempt_count = CASE "
        "WHEN processing_status = 'received' AND processed_at IS NULL THEN 0 "
        "ELSE 1 "
        "END"
    )
    op.alter_column(
        "calendly_webhook_events",
        "reducer_key",
        existing_type=sa.String(length=255),
        nullable=False,
    )
    op.create_index(
        "ix_calendly_webhook_events_reducer_key",
        "calendly_webhook_events",
        ["reducer_key"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_calendly_webhook_events_reducer_key",
        table_name="calendly_webhook_events",
    )
    op.drop_column("calendly_webhook_events", "reducer_attempt_count")
    op.drop_column("calendly_webhook_events", "reducer_key")
