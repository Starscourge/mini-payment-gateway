"""The transactional outbox: enqueue side effects, claim them, and record each attempt.

Business code enqueues in the same transaction as its state change, so a side effect is
recorded if and only if the change commits. The worker claims due rows with
FOR UPDATE SKIP LOCKED, so several workers can poll the table without taking the same row.
"""

import random
import uuid
from collections.abc import Collection
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import ids
from app.models import OutboxEvent, OutboxKind, OutboxStatus, Payment
from app.schemas.payments import PaymentOut

log = structlog.get_logger()

MAX_ATTEMPTS = 10
BASE_RETRY_DELAY = timedelta(seconds=30)
MAX_RETRY_DELAY = timedelta(hours=6)
_ERROR_MAX_LENGTH = 500


def enqueue(
    session: AsyncSession,
    *,
    kind: OutboxKind,
    merchant_id: uuid.UUID,
    payload: dict[str, Any],
    event_type: str | None = None,
) -> OutboxEvent:
    event = OutboxEvent(
        kind=kind,
        merchant_id=merchant_id,
        event_type=event_type,
        payload=payload,
        status=OutboxStatus.PENDING,
        attempts=0,
    )
    session.add(event)
    return event


def enqueue_bank_submission(session: AsyncSession, payment: Payment) -> OutboxEvent:
    return enqueue(
        session,
        kind=OutboxKind.BANK_SUBMIT,
        merchant_id=payment.merchant_id,
        payload={
            "payment_id": ids.format_id(ids.PAYMENT, payment.id),
            "amount_paise": payment.amount_paise,
            "currency": payment.currency,
        },
    )


async def enqueue_payment_event(session: AsyncSession, payment: Payment) -> OutboxEvent:
    """Queue a webhook announcing the payment's new status, e.g. ``payment.succeeded``.

    The payload is a snapshot taken now, so the event describes the payment as it was when the
    event happened, even if it changes before delivery. The event ID stays the same across
    retries, which is what lets merchants drop duplicates. Delivery arrives in milestone 4;
    until then these rows wait in the outbox.
    """
    await session.flush()  # writes the status change and reads back the new updated_at
    event_type = f"payment.{payment.status}"
    return enqueue(
        session,
        kind=OutboxKind.WEBHOOK,
        merchant_id=payment.merchant_id,
        event_type=event_type,
        payload={
            "id": ids.format_id(ids.EVENT, uuid.uuid4()),
            "type": event_type,
            "created_at": datetime.now(UTC).isoformat(),
            "data": PaymentOut.model_validate(payment).model_dump(mode="json"),
        },
    )


async def claim_next(session: AsyncSession, *, kinds: Collection[str]) -> OutboxEvent | None:
    """Lock and return the oldest due event of the given kinds, or None if nothing is due.

    SKIP LOCKED passes over rows another worker holds instead of waiting for them. The lock
    lasts until the caller's transaction ends, so the caller must record the outcome and commit
    in the same transaction.
    """
    result: OutboxEvent | None = await session.scalar(
        select(OutboxEvent)
        .where(
            OutboxEvent.status == OutboxStatus.PENDING,
            OutboxEvent.next_attempt_at <= func.now(),
            OutboxEvent.kind.in_(kinds),
        )
        .order_by(OutboxEvent.id)
        .limit(1)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )
    return result


def mark_done(event: OutboxEvent) -> None:
    event.attempts += 1
    event.status = OutboxStatus.DONE
    event.last_error = None


def record_failure(event: OutboxEvent, error: str) -> None:
    """Schedule another attempt with exponential backoff, or give up after MAX_ATTEMPTS."""
    event.attempts += 1
    event.last_error = error[:_ERROR_MAX_LENGTH]
    if event.attempts >= MAX_ATTEMPTS:
        event.status = OutboxStatus.DEAD
        log.error("outbox_event_dead", outbox_id=event.id, kind=event.kind, error=error)
        return
    event.next_attempt_at = datetime.now(UTC) + retry_delay(event.attempts)


def retry_delay(attempts: int) -> timedelta:
    """30 s after the first failure, doubling each time up to 6 h, with +/-20% jitter.

    Jitter spreads out retries that failed together (say, during a bank outage) so they don't
    all return at the same instant.
    """
    doubling: int = 2 ** (attempts - 1)
    delay = min(BASE_RETRY_DELAY * doubling, MAX_RETRY_DELAY)
    # S311: jitter only spreads load; it doesn't need to be unpredictable.
    return delay * random.uniform(0.8, 1.2)  # noqa: S311
