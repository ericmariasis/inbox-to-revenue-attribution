"""add booking link billing defaults

Revision ID: a3d2f4b8c9e1
Revises: 9f2b7e6c4a31
Create Date: 2026-03-07 22:30:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a3d2f4b8c9e1"
down_revision: Union[str, Sequence[str], None] = "9f2b7e6c4a31"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("booking_links", sa.Column("billing_amount_cents", sa.Integer(), nullable=True))
    op.add_column("booking_links", sa.Column("billing_currency", sa.String(length=3), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("booking_links", "billing_currency")
    op.drop_column("booking_links", "billing_amount_cents")
