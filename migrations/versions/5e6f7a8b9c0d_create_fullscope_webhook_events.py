"""create fullscope webhook event journal

Revision ID: 5e6f7a8b9c0d
Revises: 4d5e6f7a8b9c
Create Date: 2026-03-18 15:30:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "5e6f7a8b9c0d"
down_revision: Union[str, None] = "4d5e6f7a8b9c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "fullscope_webhook_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider_event_type", sa.String(length=128), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("appointment_id", sa.String(length=255), nullable=False),
        sa.Column("appointment_id_path", sa.String(length=128), nullable=False),
        sa.Column("calendar_id", sa.String(length=255), nullable=False),
        sa.Column("calendar_id_path", sa.String(length=128), nullable=False),
        sa.Column("workflow_id", sa.String(length=255), nullable=True),
        sa.Column("workflow_id_path", sa.String(length=128), nullable=True),
        sa.Column("tid", sa.String(length=64), nullable=True),
        sa.Column("tid_path", sa.String(length=128), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("reducer_key", sa.String(length=255), nullable=False),
        sa.Column("delivery_count", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column(
            "processing_status",
            sa.String(length=64),
            nullable=False,
            server_default=sa.text("'received'"),
        ),
        sa.Column(
            "reducer_attempt_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
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
            "appointment_id",
            "payload_sha256",
            name="uq_fullscope_webhook_events_provider_type_appointment_hash",
        ),
    )
    op.create_index(
        "ix_fullscope_webhook_events_appointment_id",
        "fullscope_webhook_events",
        ["appointment_id"],
    )
    op.create_index(
        "ix_fullscope_webhook_events_event_type",
        "fullscope_webhook_events",
        ["event_type"],
    )
    op.create_index(
        "ix_fullscope_webhook_events_processing_status",
        "fullscope_webhook_events",
        ["processing_status"],
    )
    op.create_index(
        "ix_fullscope_webhook_events_reducer_key",
        "fullscope_webhook_events",
        ["reducer_key"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_fullscope_webhook_events_reducer_key",
        table_name="fullscope_webhook_events",
    )
    op.drop_index(
        "ix_fullscope_webhook_events_processing_status",
        table_name="fullscope_webhook_events",
    )
    op.drop_index(
        "ix_fullscope_webhook_events_event_type",
        table_name="fullscope_webhook_events",
    )
    op.drop_index(
        "ix_fullscope_webhook_events_appointment_id",
        table_name="fullscope_webhook_events",
    )
    op.drop_table("fullscope_webhook_events")
