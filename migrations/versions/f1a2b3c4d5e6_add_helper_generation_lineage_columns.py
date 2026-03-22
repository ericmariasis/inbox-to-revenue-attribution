"""add helper generation lineage columns

Revision ID: f1a2b3c4d5e6
Revises: e1f2a3b4c5d6
Create Date: 2026-03-22 18:30:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "f1a2b3c4d5e6"
down_revision: Union[str, None] = "e1f2a3b4c5d6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "creator_experiment_runs",
        sa.Column("run_generator_type", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "creator_experiment_runs",
        sa.Column("run_model_name", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "creator_experiment_runs",
        sa.Column("run_config_version", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "creator_claim_snapshots",
        sa.Column("claim_generator_type", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "creator_claim_snapshots",
        sa.Column("claim_model_name", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "creator_claim_snapshots",
        sa.Column("claim_config_version", sa.String(length=128), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("creator_claim_snapshots", "claim_config_version")
    op.drop_column("creator_claim_snapshots", "claim_model_name")
    op.drop_column("creator_claim_snapshots", "claim_generator_type")
    op.drop_column("creator_experiment_runs", "run_config_version")
    op.drop_column("creator_experiment_runs", "run_model_name")
    op.drop_column("creator_experiment_runs", "run_generator_type")
