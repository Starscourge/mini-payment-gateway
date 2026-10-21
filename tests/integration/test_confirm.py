from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.ids import MERCHANT, PAYMENT, parse_id
from app.models import OutboxEvent, OutboxKind, OutboxStatus, Payment, PaymentStatus


async def outbox_rows(db_session):
    return list(await db_session.scalars(select(OutboxEvent).order_by(OutboxEvent.id)))


async def insert_payment(db_session, merchant, *, status=PaymentStatus.CREATED, expires_at):
    payment = Payment(
        merchant_id=parse_id(MERCHANT, merchant.id),
        amount_paise=50_000,
        currency="INR",
        status=status,
        refunded_paise=0,
        expires_at=expires_at,
    )
    db_session.add(payment)
    # Commit (here, a savepoint release): a request that fails rolls the shared session back,
    # which would otherwise take this uncommitted insert with it.
    await db_session.commit()
    return f"pay_{payment.id.hex}"


async def test_confirm_moves_payment_to_processing_and_queues_bank_submission(
    client, merchant, make_payment, db_session
):
    payment = await make_payment(amount_paise=12_345)

    response = await client.post(f"/v1/payments/{payment['id']}/confirm", headers=merchant.headers)

    assert response.status_code == 200
    assert response.json()["status"] == "processing"
    (event,) = await outbox_rows(db_session)
    assert event.kind == OutboxKind.BANK_SUBMIT
    assert event.status == OutboxStatus.PENDING
    assert event.merchant_id == parse_id(MERCHANT, merchant.id)
    assert event.payload == {"payment_id": payment["id"], "amount_paise": 12_345, "currency": "INR"}


async def test_confirming_twice_is_a_conflict_and_queues_one_submission(
    client, merchant, make_payment, db_session
):
    payment = await make_payment(confirm=True)

    response = await client.post(f"/v1/payments/{payment['id']}/confirm", headers=merchant.headers)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "invalid_state_transition"
    assert len(await outbox_rows(db_session)) == 1


async def test_payment_past_expiry_cannot_be_confirmed_even_before_the_sweep(
    client, merchant, db_session
):
    payment_id = await insert_payment(
        db_session, merchant, expires_at=datetime.now(UTC) - timedelta(seconds=1)
    )

    response = await client.post(f"/v1/payments/{payment_id}/confirm", headers=merchant.headers)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "payment_expired"
    assert await outbox_rows(db_session) == []
    status = await db_session.scalar(
        select(Payment.status).where(Payment.id == parse_id(PAYMENT, payment_id))
    )
    assert status == PaymentStatus.CREATED  # left for the sweep


async def test_expired_payment_cannot_be_confirmed(client, merchant, db_session):
    payment_id = await insert_payment(
        db_session,
        merchant,
        status=PaymentStatus.EXPIRED,
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )

    response = await client.post(f"/v1/payments/{payment_id}/confirm", headers=merchant.headers)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "payment_expired"


async def test_another_merchants_payment_cannot_be_confirmed(
    client, make_merchant, make_payment, db_session
):
    other = await make_merchant("Other Co")
    theirs = await make_payment()

    response = await client.post(f"/v1/payments/{theirs['id']}/confirm", headers=other.headers)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "payment_not_found"
    assert await outbox_rows(db_session) == []


@pytest.mark.parametrize("payment_id", ["nope", "key_" + "0" * 32, "pay_" + "0" * 32])
async def test_unknown_payment_cannot_be_confirmed(client, merchant, payment_id):
    response = await client.post(f"/v1/payments/{payment_id}/confirm", headers=merchant.headers)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "payment_not_found"


async def test_confirm_requires_an_api_key(client, make_payment):
    payment = await make_payment()
    response = await client.post(f"/v1/payments/{payment['id']}/confirm")
    assert response.status_code == 401
