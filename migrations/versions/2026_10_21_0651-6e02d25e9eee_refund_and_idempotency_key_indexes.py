"""refund and idempotency key indexes

Revision ID: 6e02d25e9eee
Revises: 13f84b405238
Create Date: 2026-10-21 06:51:17.481369

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6e02d25e9eee"
down_revision: str | Sequence[str] | None = "13f84b405238"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_index(
        op.f("ix_idempotency_keys_created_at"), "idempotency_keys", ["created_at"], unique=False
    )
    op.create_index(op.f("ix_refunds_payment_id"), "refunds", ["payment_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_refunds_payment_id"), table_name="refunds")
    op.drop_index(op.f("ix_idempotency_keys_created_at"), table_name="idempotency_keys")
