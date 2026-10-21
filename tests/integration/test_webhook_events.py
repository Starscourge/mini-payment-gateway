"""GET /v1/webhook-events (the delivery log) and POST /v1/webhook-events/{id}/redeliver."""

from datetime import UTC, datetime, timedelta
from functools import partial

import httpx
import pytest
from sqlalchemy import update

from app.models import OutboxEvent, OutboxKind, OutboxStatus
from app.security.ssrf import UrlPolicy
from app.worker import main as worker
from app.worker.handlers import deliver_webhook
from tests.helpers import settle_payment


@pytest.fixture
async def shop(make_merchant):
    return await make_merchant("Shop", webhook_url="https://shop.example/webhooks")


@pytest.fixture
def endpoint_statuses():
    return [200]


@pytest.fixture
async def handlers(endpoint_statuses):
    def answer(request):
        return httpx.Response(
            endpoint_statuses.pop(0) if len(endpoint_statuses) > 1 else endpoint_statuses[0]
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
        policy = UrlPolicy(require_https=False, allow_private=False)
        yield {OutboxKind.WEBHOOK: partial(deliver_webhook, http=client, policy=policy)}


async def log(client, merchant, **params):
    response = await client.get("/v1/webhook-events", params=params, headers=merchant.headers)
    assert response.status_code == 200, response.text
    return response.json()


async def kill_all(db_session):
    """Mark every webhook event dead, as if delivery had failed ten times."""
    await db_session.execute(
        update(OutboxEvent)
        .where(OutboxEvent.kind == OutboxKind.WEBHOOK)
        .values(status=OutboxStatus.DEAD, attempts=10, last_error="merchant returned HTTP 500")
    )
    await db_session.commit()


async def test_log_lists_events_newest_first_with_their_attempts(
    client, shop, bank_secret, handlers, db_session
):
    first = await settle_payment(client, shop, bank_secret, amount_paise=100)
    second = await settle_payment(client, shop, bank_secret, amount_paise=200)
    await worker.process_next_event(db_session, handlers)  # delivers the older event

    events = (await log(client, shop))["data"]

    assert [event["payload"]["data"]["id"] for event in events] == [second["id"], first["id"]]
    newest, oldest = events
    assert newest["id"].startswith("evt_")
    assert newest["type"] == "payment.succeeded"
    assert (newest["status"], newest["attempts"], newest["deliveries"]) == ("pending", 0, [])
    assert newest["next_attempt_at"] is not None
    assert (oldest["status"], oldest["attempts"]) == ("done", 1)
    assert oldest["next_attempt_at"] is None
    (attempt,) = oldest["deliveries"]
    assert (attempt["http_status"], attempt["error"]) == (200, None)


async def test_log_filters_by_status_and_pages(client, shop, bank_secret, handlers, db_session):
    for amount in (100, 200, 300):
        await settle_payment(client, shop, bank_secret, amount_paise=amount)
    await worker.process_next_event(db_session, handlers)

    pending = await log(client, shop, status="pending")
    done = await log(client, shop, status="done")
    page_one = await log(client, shop, limit=2)
    page_two = await log(client, shop, limit=2, cursor=page_one["next_cursor"])

    assert [len(pending["data"]), len(done["data"])] == [2, 1]
    assert page_two["next_cursor"] is None
    ids = [event["id"] for event in page_one["data"] + page_two["data"]]
    assert len(set(ids)) == 3


async def test_another_merchants_events_are_invisible(client, shop, merchant, bank_secret):
    await settle_payment(client, shop, bank_secret)
    shops_event = (await log(client, shop))["data"][0]

    assert (await log(client, merchant))["data"] == []
    response = await client.post(
        f"/v1/webhook-events/{shops_event['id']}/redeliver", headers=merchant.headers
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "webhook_event_not_found"


async def test_dead_event_can_be_redelivered(client, shop, bank_secret, handlers, db_session):
    await settle_payment(client, shop, bank_secret)
    await kill_all(db_session)
    event_id = (await log(client, shop))["data"][0]["id"]
    before = datetime.now(UTC)

    response = await client.post(f"/v1/webhook-events/{event_id}/redeliver", headers=shop.headers)

    assert response.status_code == 200
    event = response.json()
    assert (event["status"], event["attempts"]) == ("pending", 0)
    due = datetime.fromisoformat(event["next_attempt_at"])
    assert before - timedelta(seconds=1) <= due <= datetime.now(UTC)
    # The worker picks it up straight away, and this time the endpoint answers.
    assert await worker.process_next_event(db_session, handlers)
    assert (await log(client, shop))["data"][0]["status"] == "done"


async def test_delivered_event_cannot_be_redelivered(
    client, shop, bank_secret, handlers, db_session
):
    await settle_payment(client, shop, bank_secret)
    await worker.process_next_event(db_session, handlers)
    event_id = (await log(client, shop))["data"][0]["id"]

    response = await client.post(f"/v1/webhook-events/{event_id}/redeliver", headers=shop.headers)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "webhook_event_delivered"


@pytest.mark.parametrize("event_id", ["evt_" + "0" * 32, "evt_nope", "pay_" + "0" * 32])
async def test_unknown_event_cannot_be_redelivered(client, shop, event_id):
    response = await client.post(f"/v1/webhook-events/{event_id}/redeliver", headers=shop.headers)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "webhook_event_not_found"


@pytest.mark.parametrize(
    ("params", "status_code", "code"),
    [
        ({"status": "failed"}, 422, "validation_error"),
        ({"limit": 0}, 422, "validation_error"),
        ({"cursor": "not-a-cursor"}, 400, "invalid_cursor"),
    ],
)
async def test_log_rejects_bad_parameters(client, shop, params, status_code, code):
    response = await client.get("/v1/webhook-events", params=params, headers=shop.headers)
    assert response.status_code == status_code
    assert response.json()["error"]["code"] == code
