"""add content authoritative extraction artifact pointer

Revision ID: 2d3e4f5a6b7c
Revises: 1c2d3e4f5a6b
Create Date: 2026-03-11 16:30:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "2d3e4f5a6b7c"
down_revision: Union[str, None] = "1c2d3e4f5a6b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "content",
        sa.Column(
            "authoritative_extraction_artifact_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_content_authoritative_extraction_artifact_id",
        "content",
        "content_extraction_artifacts",
        ["authoritative_extraction_artifact_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_content_authoritative_extraction_artifact_id",
        "content",
        type_="foreignkey",
    )
    op.drop_column("content", "authoritative_extraction_artifact_id")
