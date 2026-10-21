"""Double-entry postings and balances.

Every movement of money is a ledger transaction whose entries sum to zero: positive amounts
are debits, negative amounts are credits. Balances are never stored; they are summed from the
entries, so a bug shows up as an unbalanced transaction rather than a silently wrong number.

Accounts:
- ``bank_clearing`` (one system account, no merchant): money the bank owes the gateway.
- ``merchant_balance`` (one per merchant): money the gateway owes the merchant.
"""

import uuid
from collections.abc import Sequence
from typing import NamedTuple

from sqlalchemy import BigInteger, cast, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app import ids
from app.models import LedgerAccount, LedgerEntry, LedgerTransaction, Payment

BANK_CLEARING = "bank_clearing"
MERCHANT_BALANCE = "merchant_balance"


class UnbalancedTransaction(Exception):
    """A posting that would break double entry. Always a bug, so it is never shown to clients."""


class Entry(NamedTuple):
    account_id: uuid.UUID
    amount_paise: int  # + debit, - credit


async def account_id(
    session: AsyncSession, *, merchant_id: uuid.UUID | None, code: str
) -> uuid.UUID:
    """Return the account's ID, creating the account on first use.

    Accounts are created lazily rather than with each merchant, so merchants created before the
    ledger existed need no backfill. If two transactions create the same account at once, the
    unique constraint makes the second INSERT wait for the first and then do nothing, and the
    SELECT that follows finds the committed row.
    """
    query = select(LedgerAccount.id).where(
        LedgerAccount.merchant_id.is_(None)
        if merchant_id is None
        else LedgerAccount.merchant_id == merchant_id,
        LedgerAccount.code == code,
    )
    existing = await session.scalar(query)
    if existing is not None:
        return existing
    await session.execute(
        insert(LedgerAccount)
        .values(id=uuid.uuid4(), merchant_id=merchant_id, code=code)
        .on_conflict_do_nothing(index_elements=["merchant_id", "code"])
    )
    created = await session.scalar(query)
    if created is None:  # the conflicting row exists, so this can only be a bug
        raise RuntimeError(f"ledger account {code!r} vanished after insert")
    return created


async def post(
    session: AsyncSession,
    *,
    description: str,
    entries: Sequence[Entry],
    payment_id: uuid.UUID | None = None,
    refund_id: uuid.UUID | None = None,
) -> LedgerTransaction:
    """Write one balanced ledger transaction, or raise before writing anything."""
    if len(entries) < 2:
        raise UnbalancedTransaction("a ledger transaction needs at least two entries")
    if any(entry.amount_paise == 0 for entry in entries):
        raise UnbalancedTransaction("ledger entries must be non-zero")
    if (total := sum(entry.amount_paise for entry in entries)) != 0:
        raise UnbalancedTransaction(f"entries sum to {total}, not 0")

    transaction = LedgerTransaction(
        payment_id=payment_id, refund_id=refund_id, description=description
    )
    session.add(transaction)
    await session.flush()  # assigns transaction.id, and inserts it before its entries
    session.add_all(
        LedgerEntry(
            transaction_id=transaction.id,
            account_id=entry.account_id,
            amount_paise=entry.amount_paise,
        )
        for entry in entries
    )
    await session.flush()
    return transaction


async def post_payment_succeeded(session: AsyncSession, payment: Payment) -> LedgerTransaction:
    """The bank now owes us the amount (debit clearing), and we owe the merchant (credit)."""
    clearing = await account_id(session, merchant_id=None, code=BANK_CLEARING)
    merchant = await account_id(session, merchant_id=payment.merchant_id, code=MERCHANT_BALANCE)
    return await post(
        session,
        description=f"Payment {ids.format_id(ids.PAYMENT, payment.id)} succeeded",
        payment_id=payment.id,
        entries=[
            Entry(clearing, payment.amount_paise),
            Entry(merchant, -payment.amount_paise),
        ],
    )


async def merchant_balance(session: AsyncSession, *, merchant_id: uuid.UUID) -> int:
    """What the gateway owes the merchant, in paise (credits shown positive)."""
    # SUM over BIGINT returns NUMERIC, which asyncpg turns into a Decimal; cast it back so money
    # stays an int.
    total = await session.scalar(
        select(cast(func.coalesce(-func.sum(LedgerEntry.amount_paise), 0), BigInteger))
        .join(LedgerAccount, LedgerAccount.id == LedgerEntry.account_id)
        .where(LedgerAccount.merchant_id == merchant_id, LedgerAccount.code == MERCHANT_BALANCE)
    )
    return total or 0
