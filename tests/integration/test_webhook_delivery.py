"""The worker delivering webhooks to a fake merchant endpoint (no network involved)."""

import json
from datetime import UTC, datetime
from functools import partial

import httpx
import pytest
from sqlalchemy import select, update

from app.models import OutboxEvent, OutboxKind, OutboxStatus, WebhookDelivery
from app.security.signing import verify_signature
from app.security.ssrf import UrlPolicy
from app.services.outbox import MAX_ATTEMPTS
from app.worker import main as worker
from app.worker.handlers import deliver_webhook
from tests.helpers import PUBLIC_IP, settle_payment

LONG_AGO = datetime(2000, 1, 1, tzinfo=UTC)
WEBHOOK_URL = "https://shop.example/webhooks"


class MerchantEndpoint:
    """Plays the merchant's webhook endpoint. Answers with each of `statuses` in turn,
    repeating the last; None means the request times out."""

    def __init__(self, statuses=(200,)):
        self.statuses = list(statuses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        if status is None:
            raise httpx.ReadTimeout("timed out", request=request)
        if status in (301, 302, 307):
            return httpx.Response(status, headers={"Location": "http://127.0.0.1/admin"})
        return httpx.Response(status)

    @property
    def events(self):
        return [json.loads(request.content) for request in self.requests]


@pytest.fixture
def endpoint():
    return MerchantEndpoint()


@pytest.fixture
async def handlers(endpoint):
    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as client:
        policy = UrlPolicy(require_https=False, allow_private=False)
        yield {OutboxKind.WEBHOOK: partial(deliver_webhook, http=client, policy=policy)}


@pytest.fixture
async def shop(make_merchant):
    return await make_merchant("Shop", webhook_url=WEBHOOK_URL)


@pytest.fixture
async def event_row(client, shop, bank_secret, db_session):
    """A payment.succeeded event waiting for delivery to `shop`."""
    await settle_payment(client, shop, bank_secret, amount_paise=12_345)
    return await webhook_row(db_session)


async def webhook_row(db_session):
    return await db_session.scalar(
        select(OutboxEvent)
        .where(OutboxEvent.kind == OutboxKind.WEBHOOK)
        .execution_options(populate_existing=True)
    )


async def deliveries(db_session):
    return list(await db_session.scalars(select(WebhookDelivery).order_by(WebhookDelivery.id)))


async def make_due(db_session):
    await db_session.execute(
        update(OutboxEvent)
        .where(OutboxEvent.status == OutboxStatus.PENDING)
        .values(next_attempt_at=LONG_AGO)
    )


async def test_event_is_delivered_signed_to_the_checked_address(
    shop, event_row, endpoint, handlers, db_session
):
    assert await worker.process_next_event(db_session, handlers)

    (request,) = endpoint.requests
    # Sent to the IP the SSRF check approved, under the merchant's own hostname.
    assert str(request.url) == f"https://{PUBLIC_IP}/webhooks"
    assert request.headers["Host"] == "shop.example"
    assert request.extensions["sni_hostname"] == "shop.example"
    assert request.headers["Content-Type"] == "application/json"
    # The merchant verifies it with the helper we ship, using its own secret.
    assert verify_signature(shop.webhook_secret, request.content, request.headers["X-Signature"])
    assert not verify_signature(
        "whsec_someone_else", request.content, request.headers["X-Signature"]
    )
    assert json.loads(request.content) == event_row.payload
    assert endpoint.events[0]["type"] == "payment.succeeded"
    assert endpoint.events[0]["data"]["amount_paise"] == 12_345

    event = await webhook_row(db_session)
    assert (event.status, event.attempts) == (OutboxStatus.DONE, 1)
    (delivery,) = await deliveries(db_session)
    assert (delivery.http_status, delivery.error) == (200, None)
    assert delivery.duration_ms is not None


async def test_endpoint_that_fails_three_times_still_receives_the_event(
    shop, event_row, endpoint, handlers, db_session
):
    # Milestone 4's finish line.
    endpoint.statuses = [500, 503, None, 200]

    for _ in range(4):
        await make_due(db_session)  # skip the backoff delay
        assert await worker.process_next_event(db_session, handlers)

    event = await webhook_row(db_session)
    assert (event.status, event.attempts) == (OutboxStatus.DONE, 4)
    log = await deliveries(db_session)
    assert [delivery.http_status for delivery in log] == [500, 503, None, 200]
    assert "ReadTimeout" in (log[2].error or "")
    # At least once: the merchant saw the same event four times, each validly signed, and
    # drops the duplicates by its ID.
    assert len({event["id"] for event in endpoint.events}) == 1
    for request in endpoint.requests:
        assert verify_signature(
            shop.webhook_secret, request.content, request.headers["X-Signature"]
        )


async def test_failure_waits_for_the_backoff_before_retrying(
    event_row, endpoint, handlers, db_session
):
    endpoint.statuses = [500]

    await worker.process_next_event(db_session, handlers)

    event = await webhook_row(db_session)
    assert (event.status, event.attempts) == (OutboxStatus.PENDING, 1)
    assert event.last_error == "merchant returned HTTP 500"
    assert await worker.process_next_event(db_session, handlers) is False  # not due yet
    assert len(endpoint.requests) == 1


async def test_endpoint_that_always_fails_gets_ten_attempts_then_the_event_is_dead(
    event_row, endpoint, handlers, db_session
):
    endpoint.statuses = [500]

    for _ in range(MAX_ATTEMPTS):
        await make_due(db_session)
        assert await worker.process_next_event(db_session, handlers)

    event = await webhook_row(db_session)
    assert (event.status, event.attempts) == (OutboxStatus.DEAD, MAX_ATTEMPTS)
    assert len(await deliveries(db_session)) == MAX_ATTEMPTS
    await make_due(db_session)
    assert await worker.process_next_event(db_session, handlers) is False  # dead stays dead


async def test_redirect_is_not_followed(event_row, endpoint, handlers, db_session):
    endpoint.statuses = [302]  # pointing at 127.0.0.1, which the SSRF check would refuse

    await worker.process_next_event(db_session, handlers)

    assert len(endpoint.requests) == 1
    event = await webhook_row(db_session)
    assert event.last_error == "merchant returned HTTP 302"


async def test_hostname_that_now_resolves_privately_is_blocked(
    event_row, endpoint, handlers, dns, db_session
):
    # Public at signup, private now (DNS rebinding): the check runs again before every attempt.
    dns["shop.example"] = ["10.0.0.5"]

    await worker.process_next_event(db_session, handlers)

    assert endpoint.requests == []  # nothing was sent
    (delivery,) = await deliveries(db_session)
    assert delivery.http_status is None
    assert "blocked" in (delivery.error or "")
    event = await webhook_row(db_session)
    assert (event.status, event.attempts) == (OutboxStatus.PENDING, 1)


async def test_merchant_without_a_webhook_url_has_nothing_to_deliver(
    client, merchant, bank_secret, endpoint, handlers, db_session
):
    await settle_payment(client, merchant, bank_secret)

    await worker.process_next_event(db_session, handlers)

    assert endpoint.requests == []
    assert (await webhook_row(db_session)).status == OutboxStatus.DONE
    assert await deliveries(db_session) == []
