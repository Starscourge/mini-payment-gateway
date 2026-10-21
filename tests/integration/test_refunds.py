import pytest
from sqlalchemy import select

from app.models import (
    LedgerAccount,
    LedgerEntry,
    LedgerTransaction,
    OutboxEvent,
    OutboxKind,
    Refund,
)
from tests.helpers import idempotency_key, settle_payment, to_uuid


async def refund(client, merchant, payment_id, amount=None, *, body=None):
    if body is None:
        body = {} if amount is None else {"amount_paise": amount}
    return await client.post(
        f"/v1/payments/{payment_id}/refunds",
        json=body,
        headers=merchant.headers | idempotency_key(),
    )


async def fetch(client, merchant, path):
    response = await client.get(path, headers=merchant.headers)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
async def succeeded(client, merchant, bank_secret):
    return await settle_payment(client, merchant, bank_secret, amount_paise=50_000)


async def test_partial_refund(client, merchant, succeeded):
    response = await refund(client, merchant, succeeded["id"], 20_000)

    assert response.status_code == 201
    body = response.json()
    assert body["id"].startswith("rfd_")
    assert body["payment_id"] == succeeded["id"]
    assert (body["amount_paise"], body["status"]) == (20_000, "succeeded")
    payment = await fetch(client, merchant, f"/v1/payments/{succeeded['id']}")
    assert payment["refunded_paise"] == 20_000
    assert payment["status"] == "succeeded"  # refunds never change the status


async def test_refunds_can_add_up_to_the_full_amount(client, merchant, succeeded):
    assert (await refund(client, merchant, succeeded["id"], 20_000)).status_code == 201
    assert (await refund(client, merchant, succeeded["id"], 30_000)).status_code == 201

    response = await refund(client, merchant, succeeded["id"], 1)

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "refund_exceeds_amount"
    assert error["message"] == "The payment is already fully refunded."


async def test_omitted_amount_refunds_everything_left(client, merchant, succeeded):
    await refund(client, merchant, succeeded["id"], 20_000)

    response = await refund(client, merchant, succeeded["id"])

    assert response.json()["amount_paise"] == 30_000
    payment = await fetch(client, merchant, f"/v1/payments/{succeeded['id']}")
    assert payment["refunded_paise"] == 50_000


async def test_request_without_a_body_is_a_full_refund(client, merchant, succeeded):
    response = await client.post(
        f"/v1/payments/{succeeded['id']}/refunds", headers=merchant.headers | idempotency_key()
    )
    assert response.status_code == 201
    assert response.json()["amount_paise"] == 50_000


async def test_refund_beyond_what_is_left_is_rejected(client, merchant, succeeded):
    await refund(client, merchant, succeeded["id"], 20_000)

    response = await refund(client, merchant, succeeded["id"], 30_001)

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "refund_exceeds_amount"
    assert error["message"] == "Refund of 30001 exceeds the 30000 still refundable."
    payment = await fetch(client, merchant, f"/v1/payments/{succeeded['id']}")
    assert payment["refunded_paise"] == 20_000


@pytest.mark.parametrize("outcome", ["created", "processing", "failed"])
async def test_only_succeeded_payments_can_be_refunded(
    client, merchant, make_payment, send_bank_callback, outcome
):
    payment = await make_payment(confirm=outcome != "created")
    if outcome == "failed":
        await send_bank_callback(payment["id"], "failed")

    response = await refund(client, merchant, payment["id"], 100)

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "payment_not_refundable"
    assert outcome in error["message"]


async def test_refund_reverses_the_capture_in_the_ledger(client, merchant, succeeded, db_session):
    created = (await refund(client, merchant, succeeded["id"], 20_000)).json()

    rows = await db_session.execute(
        select(LedgerAccount.code, LedgerEntry.amount_paise)
        .join(LedgerEntry, LedgerEntry.account_id == LedgerAccount.id)
        .join(LedgerTransaction, LedgerTransaction.id == LedgerEntry.transaction_id)
        .where(LedgerTransaction.refund_id == to_uuid(created["id"]))
    )
    assert dict(rows.all()) == {"merchant_balance": 20_000, "bank_clearing": -20_000}
    assert (await fetch(client, merchant, "/v1/balance"))["balance_paise"] == 30_000


async def test_refund_queues_a_refund_succeeded_event(client, merchant, succeeded, db_session):
    created = (await refund(client, merchant, succeeded["id"], 20_000)).json()

    event = await db_session.scalar(
        select(OutboxEvent).where(
            OutboxEvent.kind == OutboxKind.WEBHOOK, OutboxEvent.event_type == "refund.succeeded"
        )
    )
    assert event is not None
    assert event.payload["data"] == created


async def test_list_refunds(client, merchant, succeeded):
    first = (await refund(client, merchant, succeeded["id"], 1_000)).json()
    second = (await refund(client, merchant, succeeded["id"], 2_000)).json()

    listed = await fetch(client, merchant, f"/v1/payments/{succeeded['id']}/refunds")

    # Both were created in this test's one transaction, so they share a created_at and
    # their relative order isn't meaningful here.
    assert sorted(listed["data"], key=lambda r: r["id"]) == sorted(
        [first, second], key=lambda r: r["id"]
    )


async def test_payment_without_refunds_lists_none(client, merchant, succeeded):
    assert await fetch(client, merchant, f"/v1/payments/{succeeded['id']}/refunds") == {"data": []}


@pytest.mark.parametrize(
    "body",
    [
        {"amount_paise": 0},
        {"amount_paise": -1},
        {"amount_paise": 10_000_001},
        {"amount_paise": 100.0},
        {"amount_paise": "100"},
        {"amount": 100},
    ],
)
async def test_refund_amount_is_validated(client, merchant, succeeded, db_session, body):
    response = await refund(client, merchant, succeeded["id"], body=body)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert await db_session.scalar(select(Refund.id)) is None


@pytest.mark.parametrize("payment_id", ["nope", "pay_" + "0" * 32])
async def test_refunding_an_unknown_payment_is_404(client, merchant, payment_id):
    created = await refund(client, merchant, payment_id, 100)
    listed = await client.get(f"/v1/payments/{payment_id}/refunds", headers=merchant.headers)

    assert (created.status_code, listed.status_code) == (404, 404)
    assert created.json()["error"]["code"] == "payment_not_found"
