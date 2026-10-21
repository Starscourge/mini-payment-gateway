"""What the worker does: one handler per outbox kind, plus periodic sweeps.

Handlers make the HTTP call and write through the services, inside the transaction that holds
the event's row lock. They never commit; the worker loop does.

The contract: a handler returns None when the side effect succeeded, or a short error message
when it failed in an expected way (the bank or the merchant was down). Either way its writes
are kept, which is how a failed webhook attempt still lands in the delivery log. An exception
means something unexpected: the handler's writes are rolled back.
"""

import json
import time
from collections.abc import Awaitable, Callable

import httpx
import structlog
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import ids
from app.models import Merchant, OutboxEvent, WebhookDelivery
from app.schemas.bank import BankSubmissionAccepted
from app.security import ssrf
from app.security.signing import SIGNATURE_HEADER, signature_header
from app.services import idempotency as idempotency_service
from app.services import payments as payments_service

log = structlog.get_logger()

Handler = Callable[[AsyncSession, OutboxEvent], Awaitable[str | None]]

USER_AGENT = "mini-payment-gateway-webhooks/0.1"


async def submit_to_bank(
    session: AsyncSession, event: OutboxEvent, *, bank: httpx.AsyncClient
) -> str | None:
    """Send the payment to the bank and store the reference from its 202 reply.

    Retrying is safe: the bank treats payment_id as its idempotency key, so a repeated
    submission returns the original reference instead of charging again. That matters most
    after a timeout, when we can't know whether the bank received the first request.
    """
    try:
        response = await bank.post("/payments", json=event.payload)
    except httpx.HTTPError as exc:  # timeouts, refused connections, protocol errors
        return f"bank request failed: {exc!r}"
    if response.status_code not in (200, 202):
        return f"bank returned HTTP {response.status_code}"
    try:
        accepted = BankSubmissionAccepted.model_validate_json(response.content)
    except ValidationError:
        return "bank reply is malformed"

    payment_id = ids.parse_id(ids.PAYMENT, event.payload["payment_id"])
    if payment_id is None:  # we wrote this payload ourselves, so this is a bug
        raise ValueError(f"bad payment_id in outbox payload: {event.payload['payment_id']!r}")
    await payments_service.record_bank_reference(
        session, payment_id=payment_id, bank_reference=accepted.bank_reference
    )
    log.info("bank_submission_accepted", bank_reference=accepted.bank_reference)
    return None


async def deliver_webhook(
    session: AsyncSession,
    event: OutboxEvent,
    *,
    http: httpx.AsyncClient,
    policy: ssrf.UrlPolicy,
) -> str | None:
    """POST the signed event to the merchant's webhook URL, and log the attempt.

    Only a 2xx answer counts as delivered. Redirects are not followed (one could point at an
    internal address), and the response body is never read, so a hostile endpoint can't make
    us download anything.
    """
    merchant = await session.scalar(select(Merchant).where(Merchant.id == event.merchant_id))
    if merchant is None:  # the foreign key makes this impossible
        raise ValueError(f"outbox event {event.id} has no merchant")
    if merchant.webhook_url is None:
        log.info("webhook_skipped", reason="merchant has no webhook_url")
        return None  # nowhere to deliver, so nothing is left to do

    body = json.dumps(event.payload, separators=(",", ":")).encode()
    headers = {
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        SIGNATURE_HEADER: signature_header(merchant.webhook_secret, body),
    }
    http_status: int | None = None
    error: str | None = None
    started = time.perf_counter()
    try:
        # Re-checked on every attempt: what the hostname resolves to can change after signup.
        target = await ssrf.pin(merchant.webhook_url, policy)
        async with http.stream(
            "POST",
            target.url,
            content=body,
            headers=headers | target.headers,
            extensions=target.extensions,
        ) as response:
            http_status = response.status_code
    except ssrf.UnsafeUrl as exc:
        error = f"blocked: {exc}"
    except httpx.HTTPError as exc:  # timeouts, refused connections, TLS failures
        error = f"request failed: {exc!r}"
    session.add(
        WebhookDelivery(
            outbox_id=event.id,
            http_status=http_status,
            duration_ms=round((time.perf_counter() - started) * 1000),
            error=error,
        )
    )
    if error is None and http_status is not None and not 200 <= http_status < 300:
        error = f"merchant returned HTTP {http_status}"
    if error is None:
        log.info("webhook_delivered", event_id=event.payload["id"], http_status=http_status)
    return error


async def expire_payments(session: AsyncSession) -> int:
    """Expire one batch of unconfirmed payments past expires_at, and commit."""
    expired = await payments_service.expire_due_payments(session)
    await session.commit()
    if expired:
        log.info("payments_expired", count=len(expired))
    return len(expired)


async def purge_idempotency_keys(session: AsyncSession) -> int:
    """Delete one batch of idempotency keys past their 24-hour retention, and commit."""
    purged = await idempotency_service.purge_expired(session)
    await session.commit()
    if purged:
        log.info("idempotency_keys_purged", count=purged)
    return purged
