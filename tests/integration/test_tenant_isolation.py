"""Merchant B can neither see nor touch merchant A's data, and can't even learn it exists:
every answer is the same 404 that a payment ID nobody owns gets."""

import pytest

from tests.helpers import idempotency_key, settle_payment

UNKNOWN_PAYMENT = "pay_" + "0" * 32


@pytest.fixture
async def alice(make_merchant):
    return await make_merchant("Alice's Shop")


@pytest.fixture
async def bob(make_merchant):
    return await make_merchant("Bob's Shop")


@pytest.fixture
async def alices_payment(client, alice, bank_secret):
    payment = await settle_payment(client, alice, bank_secret, amount_paise=50_000)
    response = await client.post(
        f"/v1/payments/{payment['id']}/refunds",
        json={"amount_paise": 1_000},
        headers=alice.headers | idempotency_key(),
    )
    assert response.status_code == 201
    return payment


ATTEMPTS = {
    "get payment": ("GET", "/v1/payments/{id}", None),
    "confirm": ("POST", "/v1/payments/{id}/confirm", None),
    "refund": ("POST", "/v1/payments/{id}/refunds", {"amount_paise": 100}),
    "list refunds": ("GET", "/v1/payments/{id}/refunds", None),
}


def without_request_id(response):
    error = response.json()["error"]
    return response.status_code, error["code"], error["message"]


@pytest.mark.parametrize("attempt", ATTEMPTS)
async def test_another_merchants_payment_looks_like_it_does_not_exist(
    client, alice, bob, alices_payment, attempt
):
    method, path, body = ATTEMPTS[attempt]

    async def send(payment_id):
        return await client.request(
            method,
            path.format(id=payment_id),
            json=body,
            headers=bob.headers | (idempotency_key() if method == "POST" else {}),
        )

    theirs = await send(alices_payment["id"])
    nobodys = await send(UNKNOWN_PAYMENT)

    assert theirs.status_code == 404
    assert without_request_id(theirs) == without_request_id(nobodys)
    # And nothing changed on Alice's side.
    after = await client.get(f"/v1/payments/{alices_payment['id']}", headers=alice.headers)
    assert after.json()["refunded_paise"] == 1_000


async def test_lists_and_balances_show_only_your_own(client, alice, bob, alices_payment):
    bobs_list = await client.get("/v1/payments", headers=bob.headers)
    bobs_balance = await client.get("/v1/balance", headers=bob.headers)
    alices_balance = await client.get("/v1/balance", headers=alice.headers)

    assert bobs_list.json()["data"] == []
    assert bobs_balance.json()["balance_paise"] == 0
    assert alices_balance.json()["balance_paise"] == 49_000


async def test_another_merchants_api_key_cannot_be_revoked(client, alice, bob):
    response = await client.delete(f"/v1/api-keys/{alice.api_key_id}", headers=bob.headers)

    assert response.status_code == 404
    still_works = await client.get("/v1/payments", headers=alice.headers)
    assert still_works.status_code == 200


async def test_reusing_another_merchants_idempotency_key_replays_nothing(client, alice, bob):
    headers = idempotency_key("order-1")
    alices = await client.post(
        "/v1/payments", json={"amount_paise": 500}, headers=alice.headers | headers
    )
    bobs = await client.post(
        "/v1/payments", json={"amount_paise": 500}, headers=bob.headers | headers
    )

    assert bobs.status_code == 201
    assert "Idempotent-Replayed" not in bobs.headers
    assert bobs.json()["id"] != alices.json()["id"]
