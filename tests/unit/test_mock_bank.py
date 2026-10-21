"""The mock bank in-process: its callbacks are captured instead of sent over the network."""

import json
import random

import httpx
import pytest
from pydantic import ValidationError

from app.security.signing import verify_signature
from mock_bank.main import FAILURE_REASONS, BankSettings, create_app

SECRET = "test-bank-callback-secret"
PAYMENT_ID = "pay_" + "1" * 32


class CallbackSink:
    """Plays the gateway's callback endpoint: records requests and answers with each of
    `statuses` in turn (repeating the last). A status of None means the connection fails."""

    def __init__(self, statuses=(200,)):
        self.statuses = list(statuses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        if status is None:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(status)

    @property
    def bodies(self):
        return [json.loads(request.content) for request in self.requests]


def bank_settings(**overrides):
    defaults = {
        "callback_secret": SECRET,
        "failure_rate": 0.0,
        "duplicate_rate": 0.0,
        "lost_response_rate": 0.0,
        "min_delay_seconds": 0.0,
        "max_delay_seconds": 0.0,
        "callback_retry_seconds": 0.0,
        "log_level": "WARNING",
        "log_format": "console",
    }
    # model_validate skips the environment and .env, like tests/unit/test_config.py.
    return BankSettings.model_validate(defaults | overrides)


@pytest.fixture
def sink():
    return CallbackSink()


@pytest.fixture
async def make_bank(sink):
    """Returns (client, bank) for a bank built with the given settings overrides."""
    opened = []

    async def make(**overrides):
        app = create_app(
            bank_settings(**overrides),
            callback_transport=httpx.MockTransport(sink),
            rng=random.Random(1),
        )
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bank")
        opened.append((client, app.state.bank))
        return client, app.state.bank

    yield make
    for client, bank in opened:
        await client.aclose()
        await bank.aclose()


async def submit(client, payment_id=PAYMENT_ID, amount_paise=50_000):
    return await client.post(
        "/payments",
        json={"payment_id": payment_id, "amount_paise": amount_paise, "currency": "INR"},
    )


async def test_submission_is_accepted_and_settled_with_a_signed_callback(make_bank, sink):
    client, bank = await make_bank()

    response = await submit(client)
    await bank.drain()

    assert response.status_code == 202
    reference = response.json()["bank_reference"]
    assert reference.startswith("bnk_")
    assert sink.bodies == [
        {
            "payment_id": PAYMENT_ID,
            "bank_reference": reference,
            "status": "succeeded",
            "failure_reason": None,
        }
    ]
    request = sink.requests[0]
    assert str(request.url) == "http://localhost:8000/internal/bank/callback"
    assert verify_signature(SECRET, request.content, request.headers["X-Signature"])


async def test_payment_id_is_the_idempotency_key(make_bank, sink):
    client, bank = await make_bank()

    first = await submit(client)
    second = await submit(client)
    await bank.drain()

    assert first.json()["bank_reference"] == second.json()["bank_reference"]
    assert len(sink.requests) == 1  # settled once, however many times it was submitted


async def test_declined_payment_reports_a_reason(make_bank, sink):
    client, bank = await make_bank(failure_rate=1.0)

    await submit(client)
    await bank.drain()

    (body,) = sink.bodies
    assert body["status"] == "failed"
    assert body["failure_reason"] in FAILURE_REASONS


async def test_duplicate_callbacks_are_identical(make_bank, sink):
    client, bank = await make_bank(duplicate_rate=1.0)

    await submit(client)
    await bank.drain()

    assert len(sink.requests) == 2
    assert sink.bodies[0] == sink.bodies[1]


async def test_lost_response_still_settles_the_payment(make_bank, sink):
    client, bank = await make_bank(lost_response_rate=1.0)

    response = await submit(client)
    await bank.drain()

    assert response.status_code == 503
    assert PAYMENT_ID in bank.submissions
    assert len(sink.requests) == 1


async def test_callback_is_retried_until_the_gateway_accepts_it(make_bank, sink):
    sink.statuses = [500, 404, 200]
    client, bank = await make_bank()

    await submit(client)
    await bank.drain()

    assert len(sink.requests) == 3
    for request in sink.requests:
        assert verify_signature(SECRET, request.content, request.headers["X-Signature"])


async def test_callback_is_abandoned_after_the_last_attempt(make_bank, sink):
    sink.statuses = [503]
    client, bank = await make_bank(callback_attempts=3)

    await submit(client)
    await bank.drain()

    assert len(sink.requests) == 3


async def test_unreachable_gateway_is_retried(make_bank, sink):
    sink.statuses = [None, 200]
    client, bank = await make_bank()

    await submit(client)
    await bank.drain()

    assert len(sink.requests) == 2


@pytest.mark.parametrize(
    "body",
    [
        {"payment_id": "not-a-payment", "amount_paise": 100, "currency": "INR"},
        {"payment_id": PAYMENT_ID, "amount_paise": 0, "currency": "INR"},
        {"payment_id": PAYMENT_ID, "amount_paise": 100, "currency": "USD"},
    ],
)
async def test_invalid_submission_is_rejected(make_bank, sink, body):
    client, _ = await make_bank()
    response = await client.post("/payments", json=body)
    assert response.status_code == 422
    assert sink.requests == []


async def test_health(make_bank):
    client, _ = await make_bank()
    assert (await client.get("/health")).json() == {"status": "ok"}


def test_delay_range_must_be_ordered():
    with pytest.raises(ValidationError, match="MIN_DELAY"):
        bank_settings(min_delay_seconds=2.0, max_delay_seconds=1.0)
