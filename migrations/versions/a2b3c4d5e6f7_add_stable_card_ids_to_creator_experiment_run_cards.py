"""add stable card ids to creator experiment run cards

Revision ID: a2b3c4d5e6f7
Revises: f1a2b3c4d5e6
Create Date: 2026-03-22 20:15:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a2b3c4d5e6f7"
down_revision: Union[str, None] = "f1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "creator_experiment_run_cards",
        sa.Column("card_id", sa.String(length=128), nullable=True),
    )
    op.create_unique_constraint(
        "uq_creator_experiment_run_cards_run_card_id",
        "creator_experiment_run_cards",
        ["run_id", "card_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_creator_experiment_run_cards_run_card_id",
        "creator_experiment_run_cards",
        type_="unique",
    )
    op.drop_column("creator_experiment_run_cards", "card_id")
