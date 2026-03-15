"""create shared rate limit events

Revision ID: 1a2b3c4d5e6f
Revises: 0f1e2d3c4b5a
Create Date: 2026-03-15 09:15:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "1a2b3c4d5e6f"
down_revision: Union[str, None] = "0f1e2d3c4b5a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "shared_rate_limit_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("namespace", sa.String(length=64), nullable=False),
        sa.Column("bucket_key", sa.String(length=512), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_shared_rate_limit_events_namespace_bucket_observed_at",
        "shared_rate_limit_events",
        ["namespace", "bucket_key", "observed_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_shared_rate_limit_events_namespace_bucket_observed_at",
        table_name="shared_rate_limit_events",
    )
    op.drop_table("shared_rate_limit_events")
