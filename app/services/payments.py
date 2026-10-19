"""The payment state machine, plus creating, fetching and listing payments."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import Conflict, PaymentNotFound
from app.models import Payment, PaymentStatus
from app.services.pagination import decode_cursor, encode_cursor

# Status only moves forward. Refunds never change it; they raise refunded_paise instead.
ALLOWED_TRANSITIONS: dict[PaymentStatus, frozenset[PaymentStatus]] = {
    PaymentStatus.CREATED: frozenset({PaymentStatus.PROCESSING, PaymentStatus.EXPIRED}),
    PaymentStatus.PROCESSING: frozenset({PaymentStatus.SUCCEEDED, PaymentStatus.FAILED}),
    PaymentStatus.SUCCEEDED: frozenset(),
    PaymentStatus.FAILED: frozenset(),
    PaymentStatus.EXPIRED: frozenset(),
}
FINAL_STATUSES = frozenset(status for status, nxt in ALLOWED_TRANSITIONS.items() if not nxt)


def can_transition(current: PaymentStatus, target: PaymentStatus) -> bool:
    return target in ALLOWED_TRANSITIONS[current]


def transition(payment: Payment, target: PaymentStatus) -> None:
    """Move `payment` to `target` or raise Conflict.

    Callers must hold the payment's row lock (SELECT ... FOR UPDATE), or a concurrent request
    could make a transition from a status that is already stale.
    """
    if not can_transition(payment.status, target):
        raise Conflict(
            "invalid_state_transition",
            f"A {payment.status} payment cannot become {target}.",
        )
    payment.status = target


async def create_payment(
    session: AsyncSession,
    *,
    merchant_id: uuid.UUID,
    amount_paise: int,
    currency: str,
    description: str | None,
    ttl: timedelta,
) -> Payment:
    payment = Payment(
        merchant_id=merchant_id,
        amount_paise=amount_paise,
        currency=currency,
        description=description,
        status=PaymentStatus.CREATED,
        refunded_paise=0,
        expires_at=datetime.now(UTC) + ttl,
    )
    session.add(payment)
    await session.flush()
    return payment


async def get_payment(
    session: AsyncSession, *, merchant_id: uuid.UUID, payment_id: uuid.UUID
) -> Payment:
    # Filtering by merchant means another merchant's payment is simply "not found" (404, not
    # 403), so its existence isn't revealed.
    payment = await session.scalar(
        select(Payment).where(Payment.id == payment_id, Payment.merchant_id == merchant_id)
    )
    if payment is None:
        raise PaymentNotFound()
    return payment


async def list_payments(
    session: AsyncSession,
    *,
    merchant_id: uuid.UUID,
    limit: int,
    cursor: str | None = None,
    status: PaymentStatus | None = None,
) -> tuple[list[Payment], str | None]:
    """Return one page of payments, newest first, and the cursor for the next page (if any)."""
    query = (
        select(Payment)
        .where(Payment.merchant_id == merchant_id)
        .order_by(Payment.created_at.desc(), Payment.id.desc())
        .limit(limit + 1)  # one extra row tells us whether another page exists
    )
    if status is not None:
        query = query.where(Payment.status == status)
    if cursor is not None:
        # Row comparison matches the (merchant_id, created_at DESC, id DESC) index.
        query = query.where(tuple_(Payment.created_at, Payment.id) < decode_cursor(cursor))

    rows = list(await session.scalars(query))
    page = rows[:limit]
    next_cursor = encode_cursor(page[-1].created_at, page[-1].id) if len(rows) > limit else None
    return page, next_cursor
