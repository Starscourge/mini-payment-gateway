"""money movement indexes

Revision ID: 13f84b405238
Revises: 417cd51b261f
Create Date: 2026-10-21 06:19:51.050048

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "13f84b405238"
down_revision: str | Sequence[str] | None = "417cd51b261f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_index(
        op.f("ix_ledger_entries_account_id"), "ledger_entries", ["account_id"], unique=False
    )
    op.create_index(
        "ledger_transactions_one_capture",
        "ledger_transactions",
        ["payment_id"],
        unique=True,
        postgresql_where=sa.text("refund_id IS NULL"),
    )
    op.create_index(
        "payments_expiry_due",
        "payments",
        ["expires_at"],
        unique=False,
        postgresql_where=sa.text("status = 'created'"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "payments_expiry_due", table_name="payments", postgresql_where=sa.text("status = 'created'")
    )
    op.drop_index(
        "ledger_transactions_one_capture",
        table_name="ledger_transactions",
        postgresql_where=sa.text("refund_id IS NULL"),
    )
    op.drop_index(op.f("ix_ledger_entries_account_id"), table_name="ledger_entries")
