from typing import Annotated

from fastapi import APIRouter, Query

from app import ids
from app.api.deps import CurrentMerchant, SessionDep
from app.api.rate_limit import RateLimited
from app.errors import WebhookEventNotFound
from app.models import OutboxStatus
from app.schemas.common import error_responses
from app.schemas.webhooks import WebhookEventList, WebhookEventOut
from app.services import webhooks as webhooks_service

router = APIRouter(
    prefix="/v1/webhook-events",
    tags=["Webhooks"],
    dependencies=[RateLimited],
    responses=error_responses(401, 429),
)


@router.get("", responses=error_responses(400, 422))
async def list_webhook_events(
    merchant: CurrentMerchant,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
    status: OutboxStatus | None = None,
) -> WebhookEventList:
    """The delivery log: your webhook events, newest first, with every delivery attempt.

    Events are delivered at least once and retried with backoff (30 s doubling to 6 h) until
    your endpoint answers 2xx; after 10 failures an event is `dead`. Drop duplicates by `id`.
    """
    page, next_cursor = await webhooks_service.list_events(
        session, merchant_id=merchant.merchant_id, limit=limit, cursor=cursor, status=status
    )
    return WebhookEventList(
        data=[WebhookEventOut.from_event(event, deliveries) for event, deliveries in page],
        next_cursor=next_cursor,
    )


@router.post("/{event_id}/redeliver", responses=error_responses(404, 409))
async def redeliver_webhook_event(
    merchant: CurrentMerchant, event_id: str, session: SessionDep
) -> WebhookEventOut:
    """Re-queue a failed or dead event for immediate delivery, with a fresh set of attempts."""
    parsed = ids.parse_id(ids.EVENT, event_id)
    if parsed is None:
        raise WebhookEventNotFound()
    event, deliveries = await webhooks_service.redeliver(
        session, merchant_id=merchant.merchant_id, event_id=ids.format_id(ids.EVENT, parsed)
    )
    await session.commit()
    return WebhookEventOut.from_event(event, deliveries)
