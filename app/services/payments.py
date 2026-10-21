"""The payment state machine and every flow that moves a payment through it."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

import structlog
from sqlalchemy import Select, func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app import ids
from app.errors import Conflict, PaymentNotFound
from app.models import Payment, PaymentStatus
from app.services import ledger, outbox
from app.services.pagination import decode_cursor, encode_cursor

log = structlog.get_logger()

BankOutcome = Literal[PaymentStatus.SUCCEEDED, PaymentStatus.FAILED]
EXPIRY_SWEEP_BATCH = 500

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


def _locked(query: Select[Payment]) -> Select[Payment]:
    # FOR UPDATE makes concurrent confirms, callbacks and refunds on one payment queue up.
    # populate_existing matters too: without it, a payment already loaded in this session keeps
    # its old attribute values, and we would hold the lock while reading stale data.
    return query.with_for_update().execution_options(populate_existing=True)


async def get_payment(
    session: AsyncSession,
    *,
    merchant_id: uuid.UUID,
    payment_id: uuid.UUID,
    for_update: bool = False,
) -> Payment:
    # Filtering by merchant means another merchant's payment is simply "not found" (404, not
    # 403), so its existence isn't revealed.
    query = select(Payment).where(Payment.id == payment_id, Payment.merchant_id == merchant_id)
    payment = await session.scalar(_locked(query) if for_update else query)
    if payment is None:
        raise PaymentNotFound()
    return payment


async def _lock_for_bank(session: AsyncSession, payment_id: uuid.UUID) -> Payment:
    # Not filtered by merchant: the bank isn't a tenant, and its messages are authenticated by
    # their HMAC signature instead.
    payment = await session.scalar(_locked(select(Payment).where(Payment.id == payment_id)))
    if payment is None:
        raise PaymentNotFound()
    return payment


async def confirm_payment(
    session: AsyncSession, *, merchant_id: uuid.UUID, payment_id: uuid.UUID
) -> Payment:
    """The customer approved the payment: mark it processing and queue the bank submission.

    Both writes share one transaction, so either the payment is processing and its submission
    is queued, or neither happened. The API never calls the bank itself; the worker does.
    """
    payment = await get_payment(
        session, merchant_id=merchant_id, payment_id=payment_id, for_update=True
    )
    # The sweep expires payments in batches, so one can be past expires_at but still 'created'.
    past_expiry = payment.expires_at <= datetime.now(UTC)
    if payment.status == PaymentStatus.EXPIRED or (
        payment.status == PaymentStatus.CREATED and past_expiry
    ):
        raise Conflict("payment_expired", "The payment expired before it was confirmed.")
    transition(payment, PaymentStatus.PROCESSING)
    outbox.enqueue_bank_submission(session, payment)
    await session.flush()
    return payment


async def record_bank_reference(
    session: AsyncSession, *, payment_id: uuid.UUID, bank_reference: str
) -> None:
    """Store the reference the bank returned for a submission.

    The first reference stored wins: the bank's callback may have arrived, and stored it,
    before the worker got here.
    """
    payment = await _lock_for_bank(session, payment_id)
    if payment.bank_reference is None:
        payment.bank_reference = bank_reference
        await session.flush()
    elif payment.bank_reference != bank_reference:
        log.error(
            "bank_reference_mismatch",
            payment_id=ids.format_id(ids.PAYMENT, payment_id),
            stored=payment.bank_reference,
            received=bank_reference,
        )


async def apply_bank_result(
    session: AsyncSession,
    *,
    payment_id: uuid.UUID,
    bank_reference: str,
    outcome: BankOutcome,
    failure_reason: str | None = None,
) -> bool:
    """Apply the bank's settlement result. Returns False if it changed nothing.

    The bank may send a callback more than once, so a payment that is already final returns
    False and is left alone. The row lock makes two copies of one callback that arrive together
    take turns: the second waits, then sees the final status.
    """
    payment = await _lock_for_bank(session, payment_id)
    if payment.status in FINAL_STATUSES:
        if payment.status != outcome:
            # Money may have moved differently from what we recorded: a case for reconciliation.
            log.error(
                "bank_result_contradicts_final_status",
                payment_id=ids.format_id(ids.PAYMENT, payment_id),
                status=payment.status,
                bank_outcome=outcome,
            )
        return False
    if payment.bank_reference not in (None, bank_reference):
        raise Conflict(
            "bank_reference_mismatch",
            "The bank reference doesn't match the one recorded for this payment.",
        )

    transition(payment, outcome)  # a payment that was never confirmed raises 409 here
    payment.bank_reference = bank_reference
    if outcome == PaymentStatus.FAILED:
        payment.failure_reason = failure_reason or "declined"
    await session.flush()
    if outcome == PaymentStatus.SUCCEEDED:
        await ledger.post_payment_succeeded(session, payment)
    await outbox.enqueue_payment_event(session, payment)
    return True


async def expire_due_payments(
    session: AsyncSession, *, limit: int = EXPIRY_SWEEP_BATCH
) -> list[Payment]:
    """Move unconfirmed payments past expires_at to expired, at most `limit` at a time.

    SKIP LOCKED passes over a payment that a confirm is working on right now: the confirm
    either moves it to processing or refuses it as expired, and a later sweep picks it up.
    """
    payments = list(
        await session.scalars(
            select(Payment)
            .where(Payment.status == PaymentStatus.CREATED, Payment.expires_at <= func.now())
            .order_by(Payment.expires_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
    )
    for payment in payments:
        transition(payment, PaymentStatus.EXPIRED)
        await outbox.enqueue_payment_event(session, payment)
    await session.flush()
    return payments


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
