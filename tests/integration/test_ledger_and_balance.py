import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.models import LedgerAccount, Payment
from app.services import ledger
from app.services.ledger import BANK_CLEARING, MERCHANT_BALANCE, Entry, UnbalancedTransaction
from tests.helpers import to_uuid


async def balance(client, merchant):
    response = await client.get("/v1/balance", headers=merchant.headers)
    assert response.status_code == 200, response.text
    return response.json()


async def settle(make_payment, send_bank_callback, owner=None, *, amount, status="succeeded"):
    payment = await make_payment(owner, amount_paise=amount, confirm=True)
    response = await send_bank_callback(
        payment["id"], status, bank_reference=f"bnk_{payment['id'][-8:]}"
    )
    assert response.json() == {"applied": True}
    return payment


async def test_new_merchant_has_zero_balance(client, merchant):
    assert await balance(client, merchant) == {"balance_paise": 0, "currency": "INR"}


async def test_balance_sums_succeeded_payments_only(
    client, merchant, make_payment, send_bank_callback
):
    await settle(make_payment, send_bank_callback, amount=50_000)
    await settle(make_payment, send_bank_callback, amount=1_234)
    await settle(make_payment, send_bank_callback, amount=99_999, status="failed")
    await make_payment(amount_paise=70_000, confirm=True)  # still processing

    assert (await balance(client, merchant))["balance_paise"] == 51_234


async def test_balance_is_per_merchant(
    client, merchant, make_merchant, make_payment, send_bank_callback
):
    other = await make_merchant("Other Co")
    await settle(make_payment, send_bank_callback, amount=50_000)
    await settle(make_payment, send_bank_callback, other, amount=700)

    assert (await balance(client, merchant))["balance_paise"] == 50_000
    assert (await balance(client, other))["balance_paise"] == 700


async def test_balance_is_an_int_not_a_decimal(
    merchant, make_payment, send_bank_callback, db_session
):
    # Postgres SUM(bigint) is NUMERIC, which asyncpg returns as Decimal unless cast back.
    await settle(make_payment, send_bank_callback, amount=500)
    total = await ledger.merchant_balance(db_session, merchant_id=to_uuid(merchant.id))
    assert type(total) is int


async def test_balance_requires_an_api_key(client):
    assert (await client.get("/v1/balance")).status_code == 401


async def test_accounts_are_created_once(merchant, db_session):
    merchant_id = to_uuid(merchant.id)
    first = await ledger.account_id(db_session, merchant_id=merchant_id, code=MERCHANT_BALANCE)
    again = await ledger.account_id(db_session, merchant_id=merchant_id, code=MERCHANT_BALANCE)
    system = await ledger.account_id(db_session, merchant_id=None, code=BANK_CLEARING)
    system_again = await ledger.account_id(db_session, merchant_id=None, code=BANK_CLEARING)

    assert first == again
    assert system == system_again
    assert await db_session.scalar(select(func.count()).select_from(LedgerAccount)) == 2


@pytest.mark.parametrize(
    "amounts",
    [
        [100, -99],  # doesn't sum to zero
        [100],  # one-sided
        [],
        [100, 0, -100],  # zero entries carry no meaning
    ],
)
async def test_unbalanced_postings_are_refused(db_session, amounts):
    account = await ledger.account_id(db_session, merchant_id=None, code=BANK_CLEARING)
    with pytest.raises(UnbalancedTransaction):
        await ledger.post(
            db_session,
            description="bad",
            entries=[Entry(account, amount) for amount in amounts],
        )


async def test_database_refuses_a_second_capture_of_one_payment(
    make_payment, send_bank_callback, db_session
):
    # The final-status check stops duplicate callbacks; this index is the backstop if it ever
    # has a bug.
    payment = await settle(make_payment, send_bank_callback, amount=500)
    stored = await db_session.get(Payment, to_uuid(payment["id"]))

    with pytest.raises(IntegrityError, match="ledger_transactions_one_capture"):
        await ledger.post_payment_succeeded(db_session, stored)
    await db_session.rollback()
