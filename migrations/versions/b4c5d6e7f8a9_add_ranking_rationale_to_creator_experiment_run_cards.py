"""add ranking rationale to creator experiment run cards

Revision ID: b4c5d6e7f8a9
Revises: a2b3c4d5e6f7
Create Date: 2026-03-23 00:10:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b4c5d6e7f8a9"
down_revision: Union[str, None] = "a2b3c4d5e6f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "creator_experiment_run_cards",
        sa.Column("ranking_rationale", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("creator_experiment_run_cards", "ranking_rationale")
