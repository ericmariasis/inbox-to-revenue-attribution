"""create billing provider switch attempts

Revision ID: e1f2a3b4c5d6
Revises: d1e2f3a4b5c6
Create Date: 2026-03-21 10:30:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "e1f2a3b4c5d6"
down_revision: Union[str, None] = "d1e2f3a4b5c6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "billing_provider_switch_attempts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("creator_id", sa.UUID(), nullable=False),
        sa.Column("source_billing_provider", sa.String(length=32), nullable=False),
        sa.Column("target_billing_provider", sa.String(length=32), nullable=False),
        sa.Column(
            "target_billing_connect_status",
            sa.String(length=32),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("target_billing_account_id", sa.String(length=255), nullable=True),
        sa.Column("target_billing_connected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "target_billing_provider_correlation_id",
            sa.String(length=255),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["creator_id"], ["creators.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "creator_id",
            name="uq_billing_provider_switch_attempts_creator_id",
        ),
    )
    op.create_index(
        "ix_billing_provider_switch_attempts_creator_id",
        "billing_provider_switch_attempts",
        ["creator_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_billing_provider_switch_attempts_creator_id",
        table_name="billing_provider_switch_attempts",
    )
    op.drop_table("billing_provider_switch_attempts")
