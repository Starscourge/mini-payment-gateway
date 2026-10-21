"""webhook event indexes

Revision ID: 3b4a539f3575
Revises: 6e02d25e9eee
Create Date: 2026-10-21 07:18:07.572630

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3b4a539f3575"
down_revision: str | Sequence[str] | None = "6e02d25e9eee"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_index(
        "outbox_event_id",
        "outbox",
        [sa.literal_column("(payload ->> 'id')")],
        unique=True,
        postgresql_where=sa.text("kind = 'webhook'"),
    )
    op.create_index(
        "outbox_webhook_events",
        "outbox",
        ["merchant_id", sa.literal_column("id DESC")],
        unique=False,
        postgresql_where=sa.text("kind = 'webhook'"),
    )
    op.create_index(
        op.f("ix_webhook_deliveries_outbox_id"), "webhook_deliveries", ["outbox_id"], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_webhook_deliveries_outbox_id"), table_name="webhook_deliveries")
    op.drop_index(
        "outbox_webhook_events", table_name="outbox", postgresql_where=sa.text("kind = 'webhook'")
    )
    op.drop_index(
        "outbox_event_id", table_name="outbox", postgresql_where=sa.text("kind = 'webhook'")
    )
