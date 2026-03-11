"""create calendly webhook event journal

Revision ID: 3e4f5a6b7c8d
Revises: 2d3e4f5a6b7c
Create Date: 2026-03-11 18:45:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "3e4f5a6b7c8d"
down_revision: Union[str, None] = "2d3e4f5a6b7c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "calendly_webhook_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("calendly_event_id", sa.String(length=255), nullable=False),
        sa.Column("provider_event_type", sa.String(length=128), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("calendly_event_id_path", sa.String(length=128), nullable=False),
        sa.Column("calendly_booking_uuid", sa.String(length=255), nullable=False),
        sa.Column("calendly_booking_uuid_path", sa.String(length=128), nullable=False),
        sa.Column("tid", sa.String(length=64), nullable=True),
        sa.Column("tid_path", sa.String(length=128), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("delivery_count", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column(
            "processing_status",
            sa.String(length=64),
            nullable=False,
            server_default=sa.text("'received'"),
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "last_received_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider_event_type",
            "calendly_event_id",
            "calendly_booking_uuid",
            name="uq_calendly_webhook_events_provider_type_event_booking",
        ),
    )
    op.create_index(
        "ix_calendly_webhook_events_booking_uuid",
        "calendly_webhook_events",
        ["calendly_booking_uuid"],
    )
    op.create_index(
        "ix_calendly_webhook_events_event_type",
        "calendly_webhook_events",
        ["event_type"],
    )
    op.create_index(
        "ix_calendly_webhook_events_processing_status",
        "calendly_webhook_events",
        ["processing_status"],
    )


def downgrade() -> None:
    op.drop_index("ix_calendly_webhook_events_processing_status", table_name="calendly_webhook_events")
    op.drop_index("ix_calendly_webhook_events_event_type", table_name="calendly_webhook_events")
    op.drop_index("ix_calendly_webhook_events_booking_uuid", table_name="calendly_webhook_events")
    op.drop_table("calendly_webhook_events")
