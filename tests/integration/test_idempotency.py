from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.models import IdempotencyKey, OutboxEvent, OutboxKind, Payment, Refund
from app.worker.handlers import purge_idempotency_keys
from tests.helpers import confirm_payment, idempotency_key, settle_payment, to_uuid


async def post_payment(client, merchant, key, body=None):
    return await client.post(
        "/v1/payments",
        json=body or {"amount_paise": 50_000},
        headers=merchant.headers | idempotency_key(key),
    )


async def count(db_session, model):
    return await db_session.scalar(select(func.count()).select_from(model))


async def test_retry_replays_the_original_response_and_creates_one_payment(
    client, merchant, db_session
):
    first = await post_payment(client, merchant, "order-1042")
    retry = await post_payment(client, merchant, "order-1042")

    assert (first.status_code, retry.status_code) == (201, 201)
    assert retry.json() == first.json()
    assert retry.headers["Idempotent-Replayed"] == "true"
    assert "Idempotent-Replayed" not in first.headers
    assert await count(db_session, Payment) == 1


async def test_replay_returns_the_original_response_even_after_the_payment_changed(
    client, merchant
):
    created = await post_payment(client, merchant, "order-1")
    await confirm_payment(client, merchant, created.json()["id"])

    retry = await post_payment(client, merchant, "order-1")

    assert retry.json()["status"] == "created"  # what the original request returned


async def test_same_key_with_a_different_body_is_422(client, merchant, db_session):
    await post_payment(client, merchant, "order-1", {"amount_paise": 50_000})
    response = await post_payment(client, merchant, "order-1", {"amount_paise": 60_000})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "idempotency_key_reused"
    assert await count(db_session, Payment) == 1


async def test_same_key_on_another_endpoint_is_422(client, merchant):
    created = await post_payment(client, merchant, "shared-key")
    response = await confirm_payment(client, merchant, created.json()["id"], key="shared-key")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "idempotency_key_reused"


async def test_reformatted_json_body_is_still_the_same_request(client, merchant, db_session):
    headers = merchant.headers | idempotency_key("order-7") | {"Content-Type": "application/json"}
    first = await client.post(
        "/v1/payments", content=b'{"amount_paise":500,"description":"x"}', headers=headers
    )
    retry = await client.post(
        "/v1/payments", content=b'{ "description": "x",  "amount_paise": 500 }', headers=headers
    )

    assert retry.json() == first.json()
    assert await count(db_session, Payment) == 1


def endpoints(payment_id):
    return [
        ("/v1/payments", {"amount_paise": 500}),
        (f"/v1/payments/{payment_id}/confirm", None),
        (f"/v1/payments/{payment_id}/refunds", {}),
    ]


@pytest.mark.parametrize("endpoint", range(3))
async def test_key_is_required(client, merchant, make_payment, db_session, endpoint):
    payment = await make_payment()
    path, body = endpoints(payment["id"])[endpoint]

    response = await client.post(path, json=body, headers=merchant.headers)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "idempotency_key_required"


@pytest.mark.parametrize("key", ["", "has space", "x" * 256])
async def test_malformed_key_is_400(client, merchant, key):
    response = await client.post(
        "/v1/payments",
        json={"amount_paise": 500},
        headers=merchant.headers | {"Idempotency-Key": key},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_idempotency_key"


async def test_a_rejected_request_does_not_use_up_its_key(client, merchant, db_session):
    invalid = await post_payment(client, merchant, "order-9", {"amount_paise": 0})
    valid = await post_payment(client, merchant, "order-9", {"amount_paise": 500})

    assert invalid.status_code == 422
    assert valid.status_code == 201  # not a replay of the 422, and not a body mismatch
    assert "Idempotent-Replayed" not in valid.headers


async def test_a_business_error_does_not_use_up_its_key(
    client, merchant, make_payment, send_bank_callback, db_session
):
    # Refunding before the payment succeeds fails and changes nothing, so the same key can be
    # used once the payment is refundable.
    payment = await make_payment(confirm=True)
    path = f"/v1/payments/{payment['id']}/refunds"
    headers = merchant.headers | idempotency_key("refund-1")

    too_early = await client.post(path, json={}, headers=headers)
    await send_bank_callback(payment["id"])
    in_time = await client.post(path, json={}, headers=headers)

    assert too_early.json()["error"]["code"] == "payment_not_refundable"
    assert in_time.status_code == 201


async def test_confirm_retry_replays_and_queues_one_submission(
    client, merchant, make_payment, db_session
):
    payment = await make_payment()

    first = await confirm_payment(client, merchant, payment["id"], key="confirm-1")
    retry = await confirm_payment(client, merchant, payment["id"], key="confirm-1")

    assert (first.status_code, retry.status_code) == (200, 200)
    assert retry.json() == first.json()
    assert retry.headers["Idempotent-Replayed"] == "true"
    submissions = await db_session.scalar(
        select(func.count()).where(OutboxEvent.kind == OutboxKind.BANK_SUBMIT)
    )
    assert submissions == 1


async def test_refund_retry_replays_and_refunds_once(client, merchant, bank_secret, db_session):
    payment = await settle_payment(client, merchant, bank_secret, amount_paise=50_000)
    path = f"/v1/payments/{payment['id']}/refunds"
    headers = merchant.headers | idempotency_key("refund-1")

    first = await client.post(path, json={"amount_paise": 10_000}, headers=headers)
    retry = await client.post(path, json={"amount_paise": 10_000}, headers=headers)

    assert retry.json() == first.json()
    assert await count(db_session, Refund) == 1
    refunded = await db_session.scalar(
        select(Payment.refunded_paise).where(Payment.id == to_uuid(payment["id"]))
    )
    assert refunded == 10_000


async def test_worker_purges_keys_older_than_24_hours(client, merchant, db_session):
    now = datetime.now(UTC)
    merchant_id = to_uuid(merchant.id)
    for key, age in [("old", timedelta(hours=25)), ("recent", timedelta(hours=23))]:
        db_session.add(
            IdempotencyKey(
                merchant_id=merchant_id,
                key=key,
                request_hash="h",
                status_code=201,
                response_body={},
                created_at=now - age,
            )
        )
    await db_session.flush()

    assert await purge_idempotency_keys(db_session) == 1

    remaining = await db_session.scalars(select(IdempotencyKey.key))
    assert list(remaining) == ["recent"]
    # A purged key is free again: reusing it runs the request instead of replaying.
    response = await post_payment(client, merchant, "old", {"amount_paise": 777})
    assert response.status_code == 201
    assert "Idempotent-Replayed" not in response.headers
