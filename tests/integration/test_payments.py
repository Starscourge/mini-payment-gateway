from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from app.ids import MERCHANT, PAYMENT, format_id, parse_id
from app.models import Merchant, Payment, PaymentStatus
from tests.helpers import idempotency_key


async def create_payment(client, merchant, **overrides):
    body = {"amount_paise": 50_000, "currency": "INR", "description": "Order #1042"} | overrides
    response = await client.post(
        "/v1/payments", json=body, headers=merchant.headers | idempotency_key()
    )
    assert response.status_code == 201, response.text
    return response.json()


async def insert_payments(db_session, merchant, created_ats, status=PaymentStatus.CREATED):
    """Insert payments with chosen timestamps.

    Each test runs in one transaction, where now() never changes, so payments created through
    the API would all share a created_at. Pagination tests need distinct, known timestamps.
    """
    payments = [
        Payment(
            merchant_id=parse_id(MERCHANT, merchant.id),
            amount_paise=100 * (i + 1),
            currency="INR",
            status=status,
            refunded_paise=0,
            expires_at=created_at + timedelta(minutes=15),
            created_at=created_at,
        )
        for i, created_at in enumerate(created_ats)
    ]
    db_session.add_all(payments)
    await db_session.flush()
    return payments


def newest_first(payments):
    ordered = sorted(payments, key=lambda p: (p.created_at, p.id), reverse=True)
    return [format_id(PAYMENT, p.id) for p in ordered]


async def list_all(client, merchant, **params):
    ids, cursor, pages = [], None, 0
    while True:
        query = params | ({"cursor": cursor} if cursor else {})
        response = await client.get("/v1/payments", params=query, headers=merchant.headers)
        assert response.status_code == 200, response.text
        body = response.json()
        ids += [payment["id"] for payment in body["data"]]
        pages += 1
        cursor = body["next_cursor"]
        if cursor is None:
            return ids, pages


async def test_create_payment(client, merchant, settings):
    payment = await create_payment(client, merchant)

    assert payment["id"].startswith("pay_")
    assert payment["amount_paise"] == 50_000
    assert payment["currency"] == "INR"
    assert payment["status"] == "created"
    assert payment["refunded_paise"] == 0
    assert payment["description"] == "Order #1042"
    assert payment["failure_reason"] is None
    lifetime = datetime.fromisoformat(payment["expires_at"]) - datetime.fromisoformat(
        payment["created_at"]
    )
    assert abs(lifetime - timedelta(minutes=settings.payment_ttl_minutes)) < timedelta(minutes=1)


async def test_get_payment_returns_what_create_returned(client, merchant):
    created = await create_payment(client, merchant)
    response = await client.get(f"/v1/payments/{created['id']}", headers=merchant.headers)
    assert response.status_code == 200
    assert response.json() == created


@pytest.mark.parametrize("amount", [1, 10_000_000])
async def test_amount_limits_are_inclusive(client, merchant, amount):
    assert (await create_payment(client, merchant, amount_paise=amount))["amount_paise"] == amount


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({}, "amount_paise"),
        ({"amount_paise": 0}, "amount_paise"),
        ({"amount_paise": -100}, "amount_paise"),
        ({"amount_paise": 10_000_001}, "amount_paise"),
        ({"amount_paise": 500.0}, "amount_paise"),  # floats never become money
        ({"amount_paise": "500"}, "amount_paise"),
        ({"amount_paise": True}, "amount_paise"),
        ({"amount_paise": 500, "currency": "USD"}, "currency"),
        ({"amount_paise": 500, "description": "x" * 256}, "description"),
        ({"amount_paise": 500, "amount": 500}, "amount"),  # a typo isn't silently ignored
    ],
)
async def test_create_payment_validates_input(client, merchant, body, field):
    response = await client.post(
        "/v1/payments", json=body, headers=merchant.headers | idempotency_key()
    )
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert field in error["message"]


async def test_malformed_json_is_400(client, merchant):
    response = await client.post(
        "/v1/payments",
        content=b'{"amount_paise": ',
        headers=merchant.headers | {"Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "malformed_request"


async def test_another_merchants_payment_is_invisible(client, merchant, make_merchant):
    other = await make_merchant("Other Co")
    theirs = await create_payment(client, other)

    fetched = await client.get(f"/v1/payments/{theirs['id']}", headers=merchant.headers)
    listed = await client.get("/v1/payments", headers=merchant.headers)

    assert fetched.status_code == 404  # not 403: don't confirm the payment exists
    assert fetched.json()["error"]["code"] == "payment_not_found"
    assert listed.json()["data"] == []


@pytest.mark.parametrize("payment_id", ["nope", "pay_123", "key_" + "0" * 32, "pay_" + "0" * 32])
async def test_unknown_payment_id_is_404(client, merchant, payment_id):
    response = await client.get(f"/v1/payments/{payment_id}", headers=merchant.headers)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "payment_not_found"


async def test_empty_list(client, merchant):
    response = await client.get("/v1/payments", headers=merchant.headers)
    assert response.json() == {"data": [], "next_cursor": None}


async def test_list_pages_newest_first_without_gaps_or_duplicates(client, merchant, db_session):
    base = datetime(2026, 1, 1, tzinfo=UTC)
    minutes = [0, 1, 1, 1, 2, 3, 4]  # includes a three-way tie on created_at
    payments = await insert_payments(
        db_session, merchant, [base + timedelta(minutes=m) for m in minutes]
    )

    ids, pages = await list_all(client, merchant, limit=3)

    assert ids == newest_first(payments)
    assert pages == 3


async def test_new_payments_do_not_shift_later_pages(client, merchant, db_session):
    base = datetime(2026, 1, 1, tzinfo=UTC)
    older = await insert_payments(
        db_session, merchant, [base + timedelta(minutes=m) for m in range(4)]
    )
    first_page = (
        await client.get("/v1/payments", params={"limit": 2}, headers=merchant.headers)
    ).json()

    # Newer payments arrive between page requests; with OFFSET they would shift the window.
    await create_payment(client, merchant)
    await create_payment(client, merchant)
    second_page = (
        await client.get(
            "/v1/payments",
            params={"limit": 2, "cursor": first_page["next_cursor"]},
            headers=merchant.headers,
        )
    ).json()

    pages = [payment["id"] for payment in first_page["data"] + second_page["data"]]
    assert pages == newest_first(older)


async def test_list_filters_by_status(client, merchant, db_session):
    base = datetime(2026, 1, 1, tzinfo=UTC)
    await insert_payments(db_session, merchant, [base], status=PaymentStatus.CREATED)
    failed = await insert_payments(
        db_session, merchant, [base, base + timedelta(minutes=1)], status=PaymentStatus.FAILED
    )

    ids, _ = await list_all(client, merchant, status="failed")

    assert ids == newest_first(failed)


@pytest.mark.parametrize(
    ("params", "status_code", "code"),
    [
        ({"limit": 0}, 422, "validation_error"),
        ({"limit": 101}, 422, "validation_error"),
        ({"status": "pending"}, 422, "validation_error"),
        ({"cursor": "not-a-cursor"}, 400, "invalid_cursor"),
    ],
)
async def test_list_rejects_bad_parameters(client, merchant, params, status_code, code):
    response = await client.get("/v1/payments", params=params, headers=merchant.headers)
    assert response.status_code == status_code
    assert response.json()["error"]["code"] == code


@pytest.mark.parametrize(("amount", "refunded"), [(0, 0), (-1, 0), (500, 501), (500, -1)])
async def test_database_rejects_impossible_money_even_without_the_api(db_session, amount, refunded):
    # The CHECK constraints are the backstop if application code ever has a bug.
    merchant = Merchant(name="Acme", webhook_secret="whsec_test")
    db_session.add(merchant)
    await db_session.flush()
    db_session.add(
        Payment(
            merchant_id=merchant.id,
            amount_paise=amount,
            refunded_paise=refunded,
            currency="INR",
            status=PaymentStatus.CREATED,
            expires_at=datetime.now(UTC),
        )
    )

    with pytest.raises(IntegrityError, match="ck_payments_"):
        await db_session.flush()
    await db_session.rollback()
