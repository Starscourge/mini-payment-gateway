import asyncio
import json
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any

import httpx
import pytest
from sqlalchemy import select, update

from app.ids import MERCHANT, PAYMENT, parse_id
from app.models import OutboxEvent, OutboxKind, OutboxStatus, Payment, PaymentStatus
from app.services import merchants as merchants_service
from app.services import outbox
from app.services import payments as payments_service
from app.services.outbox import MAX_ATTEMPTS
from app.worker import main as worker
from app.worker.handlers import Handler, expire_payments, submit_to_bank
from tests.helpers import to_uuid

LONG_AGO = datetime(2000, 1, 1, tzinfo=UTC)


class FakeBank:
    """A deterministic stand-in for the bank. Set `mode` to choose how it answers."""

    def __init__(self) -> None:
        self.mode = "accept"
        self.requests: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        if self.mode == "timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        if self.mode == "unavailable":
            return httpx.Response(503)
        if self.mode == "malformed":
            return httpx.Response(202, json={"unexpected": True})
        # The same reference for the same payment, like the real bank's idempotency.
        return httpx.Response(202, json={"bank_reference": "bnk_" + body["payment_id"][-12:]})


@pytest.fixture
def bank():
    return FakeBank()


@pytest.fixture
async def handlers(bank):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(bank), base_url="http://bank"
    ) as client:
        yield {OutboxKind.BANK_SUBMIT: partial(submit_to_bank, bank=client)}


async def outbox_row(db_session, kind=OutboxKind.BANK_SUBMIT):
    return await db_session.scalar(
        select(OutboxEvent)
        .where(OutboxEvent.kind == kind)
        .execution_options(populate_existing=True)
    )


async def payment_row(db_session, payment_id):
    return await db_session.scalar(
        select(Payment)
        .where(Payment.id == parse_id(PAYMENT, payment_id))
        .execution_options(populate_existing=True)
    )


async def make_due(db_session):
    """Pretend the backoff delay has passed."""
    await db_session.execute(
        update(OutboxEvent)
        .where(OutboxEvent.status == OutboxStatus.PENDING)
        .values(next_attempt_at=LONG_AGO)
    )


async def test_submission_stores_the_bank_reference(make_payment, handlers, bank, db_session):
    payment = await make_payment(amount_paise=777, confirm=True)

    assert await worker.process_next_event(db_session, handlers) is True

    assert bank.requests == [{"payment_id": payment["id"], "amount_paise": 777, "currency": "INR"}]
    event = await outbox_row(db_session)
    assert (event.status, event.attempts, event.last_error) == (OutboxStatus.DONE, 1, None)
    stored = await payment_row(db_session, payment["id"])
    assert stored.bank_reference == "bnk_" + payment["id"][-12:]
    assert stored.status == PaymentStatus.PROCESSING  # only the bank's callback settles it


async def test_nothing_due_returns_false(handlers, bank, db_session):
    assert await worker.process_next_event(db_session, handlers) is False
    assert bank.requests == []


@pytest.mark.parametrize(
    ("mode", "error"),
    [
        ("unavailable", "bank returned HTTP 503"),
        ("timeout", "bank request failed"),
        ("malformed", "bank reply is malformed"),
    ],
)
async def test_failed_submission_is_retried_later(
    make_payment, handlers, bank, db_session, mode, error
):
    payment = await make_payment(confirm=True)
    bank.mode = mode
    before = datetime.now(UTC)

    await worker.process_next_event(db_session, handlers)

    event = await outbox_row(db_session)
    assert event.status == OutboxStatus.PENDING
    assert event.attempts == 1
    assert event.last_error is not None
    assert error in event.last_error
    assert event.next_attempt_at >= before + timedelta(seconds=24)  # 30 s minus jitter
    # Not due yet, so the next poll leaves it alone.
    assert await worker.process_next_event(db_session, handlers) is False
    assert (await payment_row(db_session, payment["id"])).bank_reference is None


async def test_retry_after_a_lost_reply_gets_the_same_reference(
    make_payment, handlers, bank, db_session
):
    payment = await make_payment(confirm=True)
    bank.mode = "timeout"  # the bank may have received it; we can't tell
    await worker.process_next_event(db_session, handlers)

    bank.mode = "accept"
    await make_due(db_session)
    await worker.process_next_event(db_session, handlers)

    assert (await outbox_row(db_session)).status == OutboxStatus.DONE
    assert (await payment_row(db_session, payment["id"])).bank_reference is not None
    assert len(bank.requests) == 2


async def test_submission_is_dead_after_ten_failures_and_payment_stays_processing(
    make_payment, handlers, bank, db_session
):
    payment = await make_payment(confirm=True)
    bank.mode = "unavailable"

    for _ in range(MAX_ATTEMPTS):
        await make_due(db_session)
        assert await worker.process_next_event(db_session, handlers) is True

    event = await outbox_row(db_session)
    assert (event.status, event.attempts) == (OutboxStatus.DEAD, MAX_ATTEMPTS)
    assert await worker.process_next_event(db_session, handlers) is False
    # We never learned what the bank did, so the payment isn't guessed to have failed.
    assert (await payment_row(db_session, payment["id"])).status == PaymentStatus.PROCESSING


async def test_crashing_handler_rolls_back_its_writes_but_records_the_failure(
    make_payment, db_session
):
    payment = await make_payment(confirm=True)

    async def crash(session, event):
        await payments_service.record_bank_reference(
            session, payment_id=to_uuid(payment["id"]), bank_reference="bnk_partial"
        )
        raise RuntimeError("bug in handler")

    await worker.process_next_event(db_session, {OutboxKind.BANK_SUBMIT: crash})

    event = await outbox_row(db_session)
    assert event.status == OutboxStatus.PENDING
    assert event.attempts == 1
    assert "bug in handler" in (event.last_error or "")
    assert (await payment_row(db_session, payment["id"])).bank_reference is None


async def test_worker_records_nothing_new_if_the_callback_stored_the_reference_first(
    make_payment, handlers, bank, send_bank_callback, db_session
):
    payment = await make_payment(confirm=True)
    reference = "bnk_" + payment["id"][-12:]
    await send_bank_callback(payment["id"], bank_reference=reference)

    await worker.process_next_event(db_session, handlers)

    assert (await outbox_row(db_session)).status == OutboxStatus.DONE
    stored = await payment_row(db_session, payment["id"])
    assert (stored.status, stored.bank_reference) == (PaymentStatus.SUCCEEDED, reference)


async def test_worker_only_claims_kinds_it_handles(
    make_payment, handlers, send_bank_callback, db_session
):
    payment = await make_payment(confirm=True)
    await worker.process_next_event(db_session, handlers)
    await send_bank_callback(payment["id"], bank_reference="bnk_" + payment["id"][-12:])

    # The payment.succeeded webhook waits for milestone 4's handler.
    assert await worker.process_next_event(db_session, handlers) is False
    assert (await outbox_row(db_session, OutboxKind.WEBHOOK)).status == OutboxStatus.PENDING


# --- Expiry sweep ------------------------------------------------------------


async def insert_payment(db_session, merchant, *, status, expires_at):
    payment = Payment(
        merchant_id=parse_id(MERCHANT, merchant.id),
        amount_paise=100,
        currency="INR",
        status=status,
        refunded_paise=0,
        expires_at=expires_at,
    )
    db_session.add(payment)
    await db_session.flush()
    return f"pay_{payment.id.hex}"


async def test_sweep_expires_only_unconfirmed_payments_past_expiry(merchant, db_session):
    now = datetime.now(UTC)
    overdue = await insert_payment(
        db_session, merchant, status=PaymentStatus.CREATED, expires_at=now - timedelta(minutes=1)
    )
    fresh = await insert_payment(
        db_session, merchant, status=PaymentStatus.CREATED, expires_at=now + timedelta(minutes=5)
    )
    processing = await insert_payment(
        db_session,
        merchant,
        status=PaymentStatus.PROCESSING,
        expires_at=now - timedelta(minutes=1),
    )

    assert await expire_payments(db_session) == 1

    statuses = {
        payment_id: (await payment_row(db_session, payment_id)).status
        for payment_id in (overdue, fresh, processing)
    }
    assert statuses == {
        overdue: PaymentStatus.EXPIRED,
        fresh: PaymentStatus.CREATED,
        processing: PaymentStatus.PROCESSING,
    }
    event = await outbox_row(db_session, OutboxKind.WEBHOOK)
    assert event.event_type == "payment.expired"
    assert event.payload["data"]["id"] == overdue
    assert await expire_payments(db_session) == 0  # nothing left to do


async def test_sweep_works_in_batches(merchant, db_session):
    for _ in range(3):
        await insert_payment(
            db_session, merchant, status=PaymentStatus.CREATED, expires_at=LONG_AGO
        )

    assert len(await payments_service.expire_due_payments(db_session, limit=2)) == 2
    assert len(await payments_service.expire_due_payments(db_session, limit=2)) == 1


# --- Several workers, real commits --------------------------------------------


async def committed_confirmed_payments(sessions, count):
    async with sessions() as session:
        merchant, _ = await merchants_service.create_merchant(
            session, name="Acme", webhook_url=None
        )
        payment_ids = []
        for _ in range(count):
            payment = await payments_service.create_payment(
                session,
                merchant_id=merchant.id,
                amount_paise=100,
                currency="INR",
                description=None,
                ttl=timedelta(minutes=15),
            )
            await payments_service.confirm_payment(
                session, merchant_id=merchant.id, payment_id=payment.id
            )
            payment_ids.append(payment.id)
        await session.commit()
    return payment_ids


async def test_skip_locked_gives_concurrent_workers_different_events(committed_sessions):
    await committed_confirmed_payments(committed_sessions, 2)
    kinds = [OutboxKind.BANK_SUBMIT]

    async with committed_sessions() as first, committed_sessions() as second:
        first_event = await outbox.claim_next(first, kinds=kinds)
        # The second worker doesn't wait for the first's lock; it takes the next row.
        second_event = await outbox.claim_next(second, kinds=kinds)
        assert first_event is not None
        assert second_event is not None
        assert first_event.id != second_event.id

        async with committed_sessions() as third:
            assert await outbox.claim_next(third, kinds=kinds) is None  # both rows are locked


async def test_worker_loop_processes_events_until_stopped(committed_sessions):
    (payment_id,) = await committed_confirmed_payments(committed_sessions, 1)
    stop = asyncio.Event()

    def bank(request):
        stop.set()  # shut down after this event; the loop must still finish it
        return httpx.Response(202, json={"bank_reference": "bnk_loop"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(bank), base_url="http://bank"
    ) as client:
        handlers: dict[str, Handler] = {
            OutboxKind.BANK_SUBMIT: partial(submit_to_bank, bank=client)
        }
        await asyncio.wait_for(worker.run(committed_sessions, handlers, stop), timeout=10)

    async with committed_sessions() as session:
        assert (
            await session.scalar(select(Payment.bank_reference).where(Payment.id == payment_id))
            == "bnk_loop"
        )
        assert await session.scalar(select(OutboxEvent.status)) == OutboxStatus.DONE
