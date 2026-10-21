"""Milestone 2's finish line: a payment goes from created to succeeded and the ledger balances.

This runs the real mock bank in-process. Its callbacks are captured, then forwarded to the
gateway once the worker has finished, because the app and the worker share one test session,
which can't serve two requests at the same time.
"""

import random
from datetime import UTC, datetime
from functools import partial

import httpx
import pytest
from sqlalchemy import update

from app.models import OutboxEvent, OutboxKind
from app.worker import main as worker
from app.worker.handlers import submit_to_bank
from mock_bank.main import BankSettings, create_app


class Gateway:
    """Holds the bank's callbacks until the test forwards them to the gateway."""

    def __init__(self) -> None:
        self.callbacks: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.callbacks.append(request)
        return httpx.Response(200)


@pytest.fixture
def held():
    return Gateway()


@pytest.fixture
async def mock_bank(bank_secret, held):
    """Returns (bank, worker handlers) for a real mock bank with the given settings."""
    opened = []

    async def make(**overrides):
        settings = BankSettings.model_validate(
            {
                "callback_secret": bank_secret,
                "failure_rate": 0.0,
                "duplicate_rate": 0.0,
                "min_delay_seconds": 0.0,
                "max_delay_seconds": 0.0,
                "log_level": "WARNING",
                "log_format": "console",
            }
            | overrides
        )
        app = create_app(
            settings, callback_transport=httpx.MockTransport(held), rng=random.Random(7)
        )
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bank")
        opened.append((client, app.state.bank))
        return app.state.bank, {OutboxKind.BANK_SUBMIT: partial(submit_to_bank, bank=client)}

    yield make
    for client, bank in opened:
        await client.aclose()
        await bank.aclose()


async def forward_callbacks(client, held):
    responses = []
    for request in held.callbacks:
        response = await client.post(
            "/internal/bank/callback",
            content=request.content,
            headers={
                "Content-Type": "application/json",
                "X-Signature": request.headers["X-Signature"],
            },
        )
        responses.append(response)
    held.callbacks.clear()
    return responses


async def get(client, merchant, path):
    response = await client.get(path, headers=merchant.headers)
    assert response.status_code == 200, response.text
    return response.json()


async def test_payment_goes_from_created_to_succeeded(
    client, merchant, make_payment, mock_bank, held, db_session
):
    bank, handlers = await mock_bank()
    payment = await make_payment(amount_paise=50_000)
    assert payment["status"] == "created"

    response = await client.post(f"/v1/payments/{payment['id']}/confirm", headers=merchant.headers)
    assert response.json()["status"] == "processing"

    assert await worker.process_next_event(db_session, handlers)
    await bank.drain()
    (callback,) = await forward_callbacks(client, held)

    assert callback.json() == {"applied": True}
    assert (await get(client, merchant, f"/v1/payments/{payment['id']}"))["status"] == "succeeded"
    assert (await get(client, merchant, "/v1/balance"))["balance_paise"] == 50_000
    # The db_session fixture then checks that every ledger transaction sums to zero.


async def test_declined_payment_ends_failed_with_nothing_owed(
    client, merchant, make_payment, mock_bank, held, db_session
):
    bank, handlers = await mock_bank(failure_rate=1.0)
    payment = await make_payment(confirm=True)

    await worker.process_next_event(db_session, handlers)
    await bank.drain()
    await forward_callbacks(client, held)

    settled = await get(client, merchant, f"/v1/payments/{payment['id']}")
    assert settled["status"] == "failed"
    assert settled["failure_reason"]
    assert (await get(client, merchant, "/v1/balance"))["balance_paise"] == 0


async def test_duplicate_callbacks_credit_the_merchant_once(
    client, merchant, make_payment, mock_bank, held, db_session
):
    bank, handlers = await mock_bank(duplicate_rate=1.0)
    await make_payment(amount_paise=20_000, confirm=True)

    await worker.process_next_event(db_session, handlers)
    await bank.drain()
    first, second = await forward_callbacks(client, held)

    assert (first.json(), second.json()) == ({"applied": True}, {"applied": False})
    assert (await get(client, merchant, "/v1/balance"))["balance_paise"] == 20_000


async def test_lost_bank_reply_still_settles_once(
    client, merchant, make_payment, mock_bank, held, db_session
):
    # The bank accepts the payment but its 202 never arrives, so the worker must retry, and
    # the callback can come before the worker knows the reference.
    bank, handlers = await mock_bank(lost_response_rate=1.0)
    payment = await make_payment(amount_paise=30_000, confirm=True)

    await worker.process_next_event(db_session, handlers)  # sees a 503
    await bank.drain()
    (callback,) = await forward_callbacks(client, held)
    assert callback.json() == {"applied": True}

    bank.settings.lost_response_rate = 0.0
    await db_session.execute(  # skip the backoff delay
        update(OutboxEvent).values(next_attempt_at=datetime(2000, 1, 1, tzinfo=UTC))
    )
    assert await worker.process_next_event(db_session, handlers)  # retry: same reference
    await bank.drain()

    assert held.callbacks == []  # the bank settled it only once
    assert len(bank.submissions) == 1
    assert (await get(client, merchant, f"/v1/payments/{payment['id']}"))["status"] == "succeeded"
    assert (await get(client, merchant, "/v1/balance"))["balance_paise"] == 30_000
