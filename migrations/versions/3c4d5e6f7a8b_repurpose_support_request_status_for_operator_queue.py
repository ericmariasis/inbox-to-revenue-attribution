"""repurpose support request status for operator queue

Revision ID: 3c4d5e6f7a8b
Revises: 2b3c4d5e6f7a
Create Date: 2026-03-15 13:15:00.000000
"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "3c4d5e6f7a8b"
down_revision: Union[str, None] = "2b3c4d5e6f7a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE support_requests
        SET status = 'submitted'
        WHERE status IN ('notification_pending', 'pending', 'notification_failed')
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE support_requests
        SET status = CASE
            WHEN notification_failed_at IS NOT NULL AND notification_sent_at IS NULL THEN 'notification_failed'
            WHEN notification_sent_at IS NOT NULL THEN 'pending'
            ELSE 'notification_pending'
        END
        """
    )
