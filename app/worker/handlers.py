"""What the worker does: one handler per outbox kind, plus the expiry sweep.

Handlers make the HTTP call and then write through the services, inside the transaction that
holds the event's row lock. They never commit; the worker loop does.
"""

from collections.abc import Awaitable, Callable

import httpx
import structlog
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app import ids
from app.models import OutboxEvent
from app.schemas.bank import BankSubmissionAccepted
from app.services import payments as payments_service

log = structlog.get_logger()

Handler = Callable[[AsyncSession, OutboxEvent], Awaitable[None]]


class TransientError(Exception):
    """An expected failure worth retrying, such as a bank timeout. Logged without a traceback."""


async def submit_to_bank(
    session: AsyncSession, event: OutboxEvent, *, bank: httpx.AsyncClient
) -> None:
    """Send the payment to the bank and store the reference from its 202 reply.

    Retrying is safe: the bank treats payment_id as its idempotency key, so a repeated
    submission returns the original reference instead of charging again. That matters most
    after a timeout, when we can't know whether the bank received the first request.
    """
    try:
        response = await bank.post("/payments", json=event.payload)
    except httpx.HTTPError as exc:  # timeouts, refused connections, protocol errors
        raise TransientError(f"bank request failed: {exc!r}") from exc
    if response.status_code not in (200, 202):
        raise TransientError(f"bank returned HTTP {response.status_code}")
    try:
        accepted = BankSubmissionAccepted.model_validate_json(response.content)
    except ValidationError as exc:
        raise TransientError("bank reply is malformed") from exc

    payment_id = ids.parse_id(ids.PAYMENT, event.payload["payment_id"])
    if payment_id is None:  # we wrote this payload ourselves, so this is a bug
        raise ValueError(f"bad payment_id in outbox payload: {event.payload['payment_id']!r}")
    await payments_service.record_bank_reference(
        session, payment_id=payment_id, bank_reference=accepted.bank_reference
    )
    log.info("bank_submission_accepted", bank_reference=accepted.bank_reference)


async def expire_payments(session: AsyncSession) -> int:
    """Expire one batch of unconfirmed payments past expires_at, and commit."""
    expired = await payments_service.expire_due_payments(session)
    await session.commit()
    if expired:
        log.info("payments_expired", count=len(expired))
    return len(expired)
