import time
from typing import Any

import pytest
from sqlalchemy import select

from app.ids import PAYMENT, parse_id
from app.models import (
    LedgerAccount,
    LedgerEntry,
    LedgerTransaction,
    OutboxEvent,
    OutboxKind,
    Payment,
)
from app.security.signing import signature_header
from app.services import payments as payments_service
from tests.helpers import to_uuid


async def ledger_postings(db_session, payment_id):
    """{account code: amount} for each ledger transaction of the payment."""
    rows = await db_session.execute(
        select(LedgerTransaction.id, LedgerAccount.code, LedgerEntry.amount_paise)
        .join(LedgerEntry, LedgerEntry.transaction_id == LedgerTransaction.id)
        .join(LedgerAccount, LedgerAccount.id == LedgerEntry.account_id)
        .where(LedgerTransaction.payment_id == parse_id(PAYMENT, payment_id))
    )
    postings: dict[Any, dict[str, int]] = {}
    for transaction_id, code, amount in rows:
        postings.setdefault(transaction_id, {})[code] = amount
    return list(postings.values())


async def webhook_events(db_session):
    return list(
        await db_session.scalars(
            select(OutboxEvent)
            .where(OutboxEvent.kind == OutboxKind.WEBHOOK)
            .order_by(OutboxEvent.id)
        )
    )


async def stored_reference(db_session, payment_id):
    return await db_session.scalar(
        select(Payment.bank_reference).where(Payment.id == parse_id(PAYMENT, payment_id))
    )


async def fetch(client, merchant, payment):
    response = await client.get(f"/v1/payments/{payment['id']}", headers=merchant.headers)
    return response.json()


@pytest.fixture
async def processing(make_payment):
    return await make_payment(amount_paise=50_000, confirm=True)


async def test_success_settles_the_payment_and_posts_the_ledger(
    client, merchant, processing, send_bank_callback, db_session
):
    response = await send_bank_callback(processing["id"], "succeeded")

    assert response.status_code == 200
    assert response.json() == {"applied": True}
    assert (await fetch(client, merchant, processing))["status"] == "succeeded"
    assert await ledger_postings(db_session, processing["id"]) == [
        {"bank_clearing": 50_000, "merchant_balance": -50_000}
    ]
    assert await stored_reference(db_session, processing["id"]) == "bnk_test_1"


async def test_success_queues_a_payment_succeeded_event(
    client, merchant, processing, send_bank_callback, db_session
):
    await send_bank_callback(processing["id"], "succeeded")

    (event,) = await webhook_events(db_session)
    assert event.event_type == "payment.succeeded"
    assert event.payload["id"].startswith("evt_")
    assert event.payload["type"] == "payment.succeeded"
    # A snapshot of the payment exactly as the API now returns it.
    assert event.payload["data"] == await fetch(client, merchant, processing)


async def test_failure_records_the_reason_and_posts_nothing(
    client, merchant, processing, send_bank_callback, db_session
):
    response = await send_bank_callback(
        processing["id"], "failed", failure_reason="insufficient_funds"
    )

    assert response.json() == {"applied": True}
    payment = await fetch(client, merchant, processing)
    assert (payment["status"], payment["failure_reason"]) == ("failed", "insufficient_funds")
    assert await ledger_postings(db_session, processing["id"]) == []
    assert [event.event_type for event in await webhook_events(db_session)] == ["payment.failed"]


async def test_failure_without_a_reason_is_recorded_as_declined(
    client, merchant, processing, send_bank_callback
):
    await send_bank_callback(processing["id"], "failed")
    assert (await fetch(client, merchant, processing))["failure_reason"] == "declined"


async def test_duplicate_callback_changes_nothing(processing, send_bank_callback, db_session):
    await send_bank_callback(processing["id"], "succeeded")
    response = await send_bank_callback(processing["id"], "succeeded")

    assert response.status_code == 200  # so the bank stops retrying
    assert response.json() == {"applied": False}
    assert len(await ledger_postings(db_session, processing["id"])) == 1
    assert len(await webhook_events(db_session)) == 1


async def test_contradicting_callback_for_a_final_payment_changes_nothing(
    client, merchant, processing, send_bank_callback, db_session
):
    await send_bank_callback(processing["id"], "succeeded")
    response = await send_bank_callback(processing["id"], "failed")

    assert response.json() == {"applied": False}
    assert (await fetch(client, merchant, processing))["status"] == "succeeded"
    assert len(await ledger_postings(db_session, processing["id"])) == 1


async def test_callback_arriving_after_the_worker_stored_the_reference(
    processing, send_bank_callback, db_session
):
    await payments_service.record_bank_reference(
        db_session, payment_id=to_uuid(processing["id"]), bank_reference="bnk_test_1"
    )
    await db_session.commit()

    response = await send_bank_callback(processing["id"], bank_reference="bnk_test_1")

    assert response.json() == {"applied": True}


async def test_callback_with_a_different_reference_is_rejected(
    client, merchant, processing, send_bank_callback, db_session
):
    await payments_service.record_bank_reference(
        db_session, payment_id=to_uuid(processing["id"]), bank_reference="bnk_real"
    )
    await db_session.commit()

    response = await send_bank_callback(processing["id"], bank_reference="bnk_other")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "bank_reference_mismatch"
    assert (await fetch(client, merchant, processing))["status"] == "processing"


async def test_callback_for_an_unconfirmed_payment_is_rejected(
    client, merchant, make_payment, send_bank_callback, db_session
):
    payment = await make_payment()  # never confirmed, so never sent to the bank

    response = await send_bank_callback(payment["id"])

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "invalid_state_transition"
    assert (await fetch(client, merchant, payment))["status"] == "created"
    assert await stored_reference(db_session, payment["id"]) is None


@pytest.mark.parametrize("payment_id", ["pay_" + "0" * 32, "pay_nope", "mer_" + "0" * 32])
async def test_callback_for_an_unknown_payment_is_404(send_bank_callback, payment_id):
    response = await send_bank_callback(payment_id)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "payment_not_found"


async def test_wrong_secret_is_rejected(client, merchant, processing, send_bank_callback):
    response = await send_bank_callback(processing["id"], secret="not-the-bank-secret")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_signature"
    assert (await fetch(client, merchant, processing))["status"] == "processing"


async def test_stale_signature_is_rejected(processing, send_bank_callback):
    response = await send_bank_callback(processing["id"], timestamp=int(time.time()) - 301)
    assert response.status_code == 401


async def test_tampered_body_is_rejected(client, processing, bank_secret):
    signed = b'{"payment_id": "%s", "bank_reference": "bnk_1", "status": "failed"}' % (
        processing["id"].encode()
    )
    tampered = signed.replace(b"failed", b"succeeded")
    response = await client.post(
        "/internal/bank/callback",
        content=tampered,
        headers={"X-Signature": signature_header(bank_secret, signed)},
    )
    assert response.status_code == 401


@pytest.mark.parametrize("headers", [{}, {"X-Signature": "t=1,v1=nonsense"}])
async def test_missing_or_malformed_signature_is_rejected(client, processing, headers):
    response = await client.post(
        "/internal/bank/callback",
        json={"payment_id": processing["id"], "bank_reference": "b", "status": "succeeded"},
        headers=headers,
    )
    assert response.status_code == 401


async def test_every_callback_is_rejected_while_no_secret_is_configured(
    app, settings, processing, send_bank_callback
):
    app.state.settings = settings.model_copy(update={"bank_callback_secret": None})
    response = await send_bank_callback(processing["id"])
    assert response.status_code == 401


async def test_signed_but_unparseable_body_is_400(send_bank_callback):
    response = await send_bank_callback("unused", body=b'{"payment_id": ')
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "malformed_request"


@pytest.mark.parametrize(
    ("body", "field"),
    [
        (b'{"payment_id": "pay_1", "bank_reference": "b", "status": "maybe"}', "status"),
        (b'{"payment_id": "pay_1", "status": "succeeded"}', "bank_reference"),
        (b'{"payment_id": "pay_1", "bank_reference": "", "status": "succeeded"}', "bank_reference"),
    ],
)
async def test_signed_but_invalid_body_is_422(send_bank_callback, body, field):
    response = await send_bank_callback("unused", body=body)
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert field in error["message"]
