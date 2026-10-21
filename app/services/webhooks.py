"""The webhook delivery log and redelivery, as merchants see them.

Webhook events are outbox rows of kind 'webhook'; the worker delivers them
(app/worker/handlers.py) and logs every attempt in webhook_deliveries.
"""

import uuid
from collections import defaultdict
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import Conflict, WebhookEventNotFound
from app.models import OutboxEvent, OutboxKind, OutboxStatus, WebhookDelivery
from app.services import outbox
from app.services.pagination import decode_id_cursor, encode_id_cursor

EventWithDeliveries = tuple[OutboxEvent, list[WebhookDelivery]]


async def list_events(
    session: AsyncSession,
    *,
    merchant_id: uuid.UUID,
    limit: int,
    cursor: str | None = None,
    status: OutboxStatus | None = None,
) -> tuple[list[EventWithDeliveries], str | None]:
    """One page of the merchant's webhook events, newest first, each with its attempts."""
    query = (
        select(OutboxEvent)
        .where(OutboxEvent.merchant_id == merchant_id, OutboxEvent.kind == OutboxKind.WEBHOOK)
        .order_by(OutboxEvent.id.desc())
        .limit(limit + 1)  # one extra row tells us whether another page exists
    )
    if status is not None:
        query = query.where(OutboxEvent.status == status)
    if cursor is not None:
        query = query.where(OutboxEvent.id < decode_id_cursor(cursor))

    rows = list(await session.scalars(query))
    page = rows[:limit]
    next_cursor = encode_id_cursor(page[-1].id) if len(rows) > limit else None
    deliveries = await _deliveries(session, [event.id for event in page])
    return [(event, deliveries[event.id]) for event in page], next_cursor


async def redeliver(
    session: AsyncSession, *, merchant_id: uuid.UUID, event_id: str
) -> EventWithDeliveries:
    """Re-queue a failed or dead event for immediate delivery, with a fresh set of attempts.

    The row lock waits out a delivery in progress (at most the 5-second HTTP timeout), so an
    event can't be re-queued just as the worker marks it delivered.
    """
    event = await session.scalar(
        select(OutboxEvent)
        .where(
            OutboxEvent.merchant_id == merchant_id,
            OutboxEvent.kind == OutboxKind.WEBHOOK,
            OutboxEvent.payload["id"].astext == event_id,  # served by the outbox_event_id index
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if event is None:
        raise WebhookEventNotFound()
    if event.status == OutboxStatus.DONE:
        raise Conflict("webhook_event_delivered", "This event was already delivered.")
    outbox.requeue(event)
    await session.flush()
    return event, (await _deliveries(session, [event.id]))[event.id]


async def _deliveries(
    session: AsyncSession, outbox_ids: Sequence[int]
) -> defaultdict[int, list[WebhookDelivery]]:
    grouped: defaultdict[int, list[WebhookDelivery]] = defaultdict(list)
    if outbox_ids:
        rows = await session.scalars(
            select(WebhookDelivery)
            .where(WebhookDelivery.outbox_id.in_(outbox_ids))
            .order_by(WebhookDelivery.id)
        )
        for delivery in rows:
            grouped[delivery.outbox_id].append(delivery)
    return grouped
