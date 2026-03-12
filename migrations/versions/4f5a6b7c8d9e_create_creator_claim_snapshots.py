"""create creator claim snapshot contract tables

Revision ID: 4f5a6b7c8d9e
Revises: 3e4f5a6b7c8d
Create Date: 2026-03-11 21:30:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "4f5a6b7c8d9e"
down_revision: Union[str, None] = "3e4f5a6b7c8d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "creator_claim_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("creator_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("content_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("authoritative_extraction_artifact_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("authoritative_fetch_snapshot_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("claim_kind", sa.String(length=64), nullable=False),
        sa.Column("claim_contract_version", sa.String(length=128), nullable=False),
        sa.Column("claim_reducer_version", sa.String(length=128), nullable=True),
        sa.Column("claim_prompt_version", sa.String(length=128), nullable=True),
        sa.Column("rendered_claim_text", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(["authoritative_extraction_artifact_id"], ["content_extraction_artifacts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["authoritative_fetch_snapshot_id"], ["content_fetch_snapshots.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["content_id"], ["content.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["creator_id"], ["creators.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_creator_claim_snapshots_claim_kind",
        "creator_claim_snapshots",
        ["claim_kind"],
    )
    op.create_index(
        "ix_creator_claim_snapshots_content_id",
        "creator_claim_snapshots",
        ["content_id"],
    )
    op.create_index(
        "ix_creator_claim_snapshots_creator_id",
        "creator_claim_snapshots",
        ["creator_id"],
    )

    op.create_table(
        "creator_claim_paid_evidence_refs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("claim_snapshot_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("booking_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("invoice_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("payment_event_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("evidence_order", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["booking_id"], ["bookings.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["claim_snapshot_id"], ["creator_claim_snapshots.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["invoice_id"], ["invoices.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["payment_event_id"], ["invoice_payment_events.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "claim_snapshot_id",
            "evidence_order",
            name="uq_creator_claim_paid_refs_snapshot_order",
        ),
        sa.UniqueConstraint(
            "claim_snapshot_id",
            "invoice_id",
            name="uq_creator_claim_paid_refs_snapshot_invoice",
        ),
    )
    op.create_index(
        "ix_creator_claim_paid_refs_invoice_id",
        "creator_claim_paid_evidence_refs",
        ["invoice_id"],
    )
    op.create_index(
        "ix_creator_claim_paid_refs_snapshot_id",
        "creator_claim_paid_evidence_refs",
        ["claim_snapshot_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_creator_claim_paid_refs_snapshot_id", table_name="creator_claim_paid_evidence_refs")
    op.drop_index("ix_creator_claim_paid_refs_invoice_id", table_name="creator_claim_paid_evidence_refs")
    op.drop_table("creator_claim_paid_evidence_refs")

    op.drop_index("ix_creator_claim_snapshots_creator_id", table_name="creator_claim_snapshots")
    op.drop_index("ix_creator_claim_snapshots_content_id", table_name="creator_claim_snapshots")
    op.drop_index("ix_creator_claim_snapshots_claim_kind", table_name="creator_claim_snapshots")
    op.drop_table("creator_claim_snapshots")
