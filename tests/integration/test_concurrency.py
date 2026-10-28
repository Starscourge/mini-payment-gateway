"""Requests sent at the same moment, racing for real in Postgres.

These use `live_client`: an app whose requests each get their own connection and really
commit, unlike the rest of the suite, which shares one rolled-back session. Like every
concurrency test here, they must pass 50 runs in a row (see the README's section on how
correctness is tested).
"""

import asyncio
from collections import Counter

import pytest
from sqlalchemy import func, select

from app.models import (
    IdempotencyKey,
    LedgerAccount,
    LedgerTransaction,
    OutboxEvent,
    OutboxKind,
    Payment,
    Refund,
)
from app.services import idempotency as idempotency_service
from app.services.ledger import MERCHANT_BALANCE
from tests.helpers import (
    confirm_payment,
    create_merchant,
    create_payment,
    idempotency_key,
    post_bank_callback,
    settle_payment,
    to_uuid,
)


@pytest.fixture
async def acme(live_client):
    return await create_merchant(live_client)


async def scalar(committed_sessions, query):
    async with committed_sessions() as session:
        return await session.scalar(query)


async def balance(client, merchant):
    response = await client.get("/v1/balance", headers=merchant.headers)
    return response.json()["balance_paise"]


async def test_parallel_creates_with_one_key_make_one_payment(
    live_client, acme, committed_sessions
):
    responses = await asyncio.gather(
        *(
            live_client.post(
                "/v1/payments",
                json={"amount_paise": 50_000},
                headers=acme.headers | idempotency_key("order-1042"),
            )
            for _ in range(10)
        )
    )

    assert [response.status_code for response in responses] == [201] * 10
    # The same JSON document every time. (Not byte-identical: replays come from a JSONB column,
    # which stores keys in its own order.)
    assert all(response.json() == responses[0].json() for response in responses)
    # One request ran; the other nine waited on its key, then replayed its response.
    assert sum("Idempotent-Replayed" in response.headers for response in responses) == 9
    assert await scalar(committed_sessions, select(func.count()).select_from(Payment)) == 1


async def test_parallel_refunds_never_exceed_the_payment(
    live_client, acme, committed_sessions, bank_secret
):
    payment = await settle_payment(live_client, acme, bank_secret, amount_paise=50_000)

    # Twenty Rs 100 refunds of a Rs 500 payment, all at once, each with its own key.
    responses = await asyncio.gather(
        *(
            live_client.post(
                f"/v1/payments/{payment['id']}/refunds",
                json={"amount_paise": 10_000},
                headers=acme.headers | idempotency_key(),
            )
            for _ in range(20)
        )
    )

    outcomes = Counter(
        response.status_code
        if response.status_code == 201
        else (response.status_code, response.json()["error"]["code"])
        for response in responses
    )
    assert outcomes == {201: 5, (409, "refund_exceeds_amount"): 15}
    refunded = await scalar(
        committed_sessions,
        select(Payment.refunded_paise).where(Payment.id == to_uuid(payment["id"])),
    )
    assert refunded == 50_000
    assert await scalar(committed_sessions, select(func.count()).select_from(Refund)) == 5
    assert await balance(live_client, acme) == 0
    # committed_sessions then checks that every ledger transaction sums to zero.


async def test_parallel_duplicate_callbacks_settle_once(
    live_client, acme, committed_sessions, bank_secret
):
    payment = await create_payment(live_client, acme, confirm=True, amount_paise=30_000)

    responses = await asyncio.gather(
        *(
            post_bank_callback(live_client, bank_secret, payment["id"], bank_reference="bnk_dup")
            for _ in range(10)
        )
    )

    assert [response.status_code for response in responses] == [200] * 10
    assert sorted(response.json()["applied"] for response in responses) == [False] * 9 + [True]
    captures = await scalar(
        committed_sessions,
        select(func.count()).where(LedgerTransaction.payment_id == to_uuid(payment["id"])),
    )
    assert captures == 1
    assert await balance(live_client, acme) == 30_000


async def test_parallel_confirms_submit_to_the_bank_once(live_client, acme, committed_sessions):
    payment = await create_payment(live_client, acme)

    responses = await asyncio.gather(
        *(confirm_payment(live_client, acme, payment["id"]) for _ in range(10))
    )

    outcomes = Counter(response.status_code for response in responses)
    assert outcomes == {200: 1, 409: 9}
    submissions = await scalar(
        committed_sessions,
        select(func.count()).where(OutboxEvent.kind == OutboxKind.BANK_SUBMIT),
    )
    assert submissions == 1


async def test_first_settlements_of_a_new_merchant_create_its_account_once(
    live_client, acme, committed_sessions, bank_secret
):
    # Each first posting may try to create the merchant's ledger account (and the system
    # account); the unique constraint makes the racers wait and then reuse the winner's row.
    payments = [await create_payment(live_client, acme, confirm=True) for _ in range(5)]

    responses = await asyncio.gather(
        *(
            post_bank_callback(
                live_client, bank_secret, payment["id"], bank_reference=f"bnk_{index}"
            )
            for index, payment in enumerate(payments)
        )
    )

    assert all(response.json() == {"applied": True} for response in responses)
    accounts = await scalar(
        committed_sessions,
        select(func.count()).where(LedgerAccount.code == MERCHANT_BALANCE),
    )
    assert accounts == 1
    assert await scalar(committed_sessions, select(func.count()).select_from(LedgerAccount)) == 2
    assert await balance(live_client, acme) == 5 * 50_000


async def test_duplicate_gets_409_while_the_original_is_still_running(
    live_client, acme, committed_sessions, monkeypatch
):
    monkeypatch.setattr(idempotency_service, "LOCK_TIMEOUT_MS", 200)
    headers = acme.headers | idempotency_key("slow-order")

    # Play the original request: claim the key in a transaction that hasn't finished yet.
    async with committed_sessions() as original:
        await idempotency_service.begin(
            original, merchant_id=to_uuid(acme.id), key="slow-order", request_hash="h"
        )
        duplicate = await live_client.post(
            "/v1/payments", json={"amount_paise": 500}, headers=headers
        )
        await original.rollback()  # the original fails, which frees the key

    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "idempotency_key_in_use"
    retry = await live_client.post("/v1/payments", json={"amount_paise": 500}, headers=headers)
    assert retry.status_code == 201
    assert await scalar(committed_sessions, select(func.count()).select_from(IdempotencyKey)) == 1
