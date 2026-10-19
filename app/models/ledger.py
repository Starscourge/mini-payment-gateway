"""Double-entry ledger: the source of truth for money.

Every ledger transaction's entries sum to zero. Positive amounts are debits, negative are
credits.
"""

import uuid

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAt, UUIDPrimaryKey


class LedgerAccount(Base):
    __tablename__ = "ledger_accounts"
    # NULLS NOT DISTINCT (Postgres 15+): system accounts have no merchant, and must still be
    # unique per code.
    __table_args__ = (UniqueConstraint("merchant_id", "code", postgresql_nulls_not_distinct=True),)

    id: Mapped[UUIDPrimaryKey]
    merchant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("merchants.id"))
    code: Mapped[str]  # 'bank_clearing', 'merchant_balance'


class LedgerTransaction(Base):
    __tablename__ = "ledger_transactions"

    id: Mapped[UUIDPrimaryKey]
    payment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("payments.id"))
    refund_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("refunds.id"))
    description: Mapped[str]
    created_at: Mapped[CreatedAt]


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"
    __table_args__ = (CheckConstraint("amount_paise <> 0", name="amount_nonzero"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    transaction_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ledger_transactions.id"))
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ledger_accounts.id"))
    amount_paise: Mapped[int] = mapped_column(BigInteger)  # + debit, - credit
