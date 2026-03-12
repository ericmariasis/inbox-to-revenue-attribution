"""create creator experiment run tables

Revision ID: 7c8d9e0f1a2b
Revises: 6b7c8d9e0f1a
Create Date: 2026-03-12 21:15:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "7c8d9e0f1a2b"
down_revision: Union[str, None] = "6b7c8d9e0f1a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "creator_experiment_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("creator_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("summary_text", sa.Text(), nullable=False),
        sa.Column("run_contract_version", sa.String(length=128), nullable=False),
        sa.Column("run_reducer_version", sa.String(length=128), nullable=True),
        sa.Column("run_prompt_version", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(["creator_id"], ["creators.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_creator_experiment_runs_created_at",
        "creator_experiment_runs",
        ["created_at"],
    )
    op.create_index(
        "ix_creator_experiment_runs_creator_id",
        "creator_experiment_runs",
        ["creator_id"],
    )
    op.create_index(
        "ix_creator_experiment_runs_status",
        "creator_experiment_runs",
        ["status"],
    )

    op.create_table(
        "creator_experiment_run_cards",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("claim_snapshot_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("content_tid", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("hypothesis", sa.Text(), nullable=False),
        sa.Column("why_this_might_work", sa.Text(), nullable=False),
        sa.Column("evidence_summary", sa.Text(), nullable=False),
        sa.Column("caution", sa.Text(), nullable=False),
        sa.Column("card_order", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["claim_snapshot_id"], ["creator_claim_snapshots.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["creator_experiment_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "run_id",
            "card_order",
            name="uq_creator_experiment_run_cards_run_order",
        ),
        sa.UniqueConstraint(
            "run_id",
            "claim_snapshot_id",
            name="uq_creator_experiment_run_cards_run_claim_snapshot",
        ),
    )
    op.create_index(
        "ix_creator_experiment_run_cards_claim_snapshot_id",
        "creator_experiment_run_cards",
        ["claim_snapshot_id"],
    )
    op.create_index(
        "ix_creator_experiment_run_cards_run_id",
        "creator_experiment_run_cards",
        ["run_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_creator_experiment_run_cards_run_id",
        table_name="creator_experiment_run_cards",
    )
    op.drop_index(
        "ix_creator_experiment_run_cards_claim_snapshot_id",
        table_name="creator_experiment_run_cards",
    )
    op.drop_table("creator_experiment_run_cards")

    op.drop_index(
        "ix_creator_experiment_runs_status",
        table_name="creator_experiment_runs",
    )
    op.drop_index(
        "ix_creator_experiment_runs_creator_id",
        table_name="creator_experiment_runs",
    )
    op.drop_index(
        "ix_creator_experiment_runs_created_at",
        table_name="creator_experiment_runs",
    )
    op.drop_table("creator_experiment_runs")
