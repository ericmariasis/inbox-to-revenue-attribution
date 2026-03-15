"""add pending magic link issuances

Revision ID: 0f1e2d3c4b5a
Revises: 7c8d9e0f1a2b
Create Date: 2026-03-14 11:15:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "0f1e2d3c4b5a"
down_revision: Union[str, None] = "7c8d9e0f1a2b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "pending_magic_link_issuances",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("token_hash", sa.String(length=255), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "token_hash",
            name="uq_pending_magic_link_issuances_token_hash",
        ),
    )
    op.create_index(
        "ix_pending_magic_link_issuances_email",
        "pending_magic_link_issuances",
        ["email"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_pending_magic_link_issuances_email",
        table_name="pending_magic_link_issuances",
    )
    op.drop_table("pending_magic_link_issuances")
