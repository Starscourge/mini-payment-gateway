"""Plain helpers shared by test modules. Fixtures live in conftest.py and mostly wrap these.

They take the client as an argument, so the concurrency tests can use them with a client for
an app that really commits.
"""

import json
import uuid
from dataclasses import dataclass
from typing import Any

from httpx import AsyncClient, Response

from app.ids import parse_id
from app.security.signing import signature_header


def to_uuid(public_id: str) -> uuid.UUID:
    """The UUID inside a public ID such as ``pay_8c1f...``, for calling services directly."""
    value = parse_id(public_id.partition("_")[0], public_id)
    assert value is not None, f"not a public ID: {public_id!r}"
    return value


def idempotency_key(key: str | None = None) -> dict[str, str]:
    """An Idempotency-Key header: a fresh random key unless one is given."""
    return {"Idempotency-Key": key or str(uuid.uuid4())}


@dataclass(frozen=True)
class MerchantAccount:
    id: str
    api_key: str
    api_key_id: str
    webhook_secret: str

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}


async def create_merchant(client: AsyncClient, name: str = "Acme Stores") -> MerchantAccount:
    response = await client.post("/v1/merchants", json={"name": name})
    assert response.status_code == 201, response.text
    body = response.json()
    return MerchantAccount(
        id=body["id"],
        api_key=body["api_key"]["key"],
        api_key_id=body["api_key"]["id"],
        webhook_secret=body["webhook_secret"],
    )


async def create_payment(
    client: AsyncClient, merchant: MerchantAccount, *, confirm: bool = False, **fields: Any
) -> dict[str, Any]:
    """Create a payment (50,000 paise unless overridden), optionally confirming it too."""
    response = await client.post(
        "/v1/payments",
        json={"amount_paise": 50_000} | fields,
        headers=merchant.headers | idempotency_key(),
    )
    assert response.status_code == 201, response.text
    payment: dict[str, Any] = response.json()
    if confirm:
        response = await confirm_payment(client, merchant, payment["id"])
        assert response.status_code == 200, response.text
        payment = response.json()
    return payment


async def confirm_payment(
    client: AsyncClient, merchant: MerchantAccount, payment_id: str, *, key: str | None = None
) -> Response:
    return await client.post(
        f"/v1/payments/{payment_id}/confirm", headers=merchant.headers | idempotency_key(key)
    )


async def post_bank_callback(
    client: AsyncClient,
    secret: str,
    payment_id: str,
    status: str = "succeeded",
    *,
    bank_reference: str = "bnk_test_1",
    failure_reason: str | None = None,
    body: bytes | None = None,
    timestamp: int | None = None,
) -> Response:
    """POST a bank callback signed the way the bank signs it. Overrides make it invalid."""
    raw = (
        body
        or json.dumps(
            {
                "payment_id": payment_id,
                "bank_reference": bank_reference,
                "status": status,
                "failure_reason": failure_reason,
            }
        ).encode()
    )
    return await client.post(
        "/internal/bank/callback",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Signature": signature_header(secret, raw, timestamp=timestamp),
        },
    )


async def settle_payment(
    client: AsyncClient, merchant: MerchantAccount, secret: str, **fields: Any
) -> dict[str, Any]:
    """Create, confirm and successfully settle a payment, as the bank would."""
    payment = await create_payment(client, merchant, confirm=True, **fields)
    response = await post_bank_callback(
        client, secret, payment["id"], bank_reference="bnk_" + payment["id"][-12:]
    )
    assert response.json() == {"applied": True}, response.text
    return payment
