import hashlib

import pytest
from pydantic import SecretStr
from sqlalchemy import select

from app.models import ApiKey
from app.security import api_keys


async def test_create_merchant_returns_its_key_and_webhook_secret(client):
    response = await client.post(
        "/v1/merchants",
        json={"name": "  Acme Stores  ", "webhook_url": "https://acme.example/webhooks"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["id"].startswith("mer_")
    assert body["name"] == "Acme Stores"
    assert body["webhook_url"] == "https://acme.example/webhooks"
    assert body["webhook_secret"].startswith("whsec_")
    key = body["api_key"]
    assert key["id"].startswith("key_")
    assert key["key"].startswith("sk_test_")
    assert key["prefix"] == key["key"][:12]
    assert key["revoked_at"] is None


async def test_api_key_is_stored_only_as_a_hash(client, db_session, merchant):
    stored = await db_session.scalar(select(ApiKey).where(ApiKey.prefix == merchant.api_key[:12]))
    assert stored.key_hash == hashlib.sha256(merchant.api_key.encode()).hexdigest()
    assert merchant.api_key not in (stored.prefix, stored.key_hash)


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"name": ""},
        {"name": "   "},
        {"name": "x" * 201},
        {"name": "Acme", "webhook_url": "not a url"},
        {"name": "Acme", "unexpected": 1},
    ],
)
async def test_create_merchant_validates_input(client, body):
    response = await client.post("/v1/merchants", json=body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_admin_token_is_required_once_configured(app, client, settings):
    app.state.settings = settings.model_copy(
        update={"admin_token": SecretStr("admin-token-for-tests")}
    )
    body = {"name": "Acme"}

    missing = await client.post("/v1/merchants", json=body)
    wrong = await client.post("/v1/merchants", json=body, headers={"X-Admin-Token": "wrong"})
    right = await client.post(
        "/v1/merchants", json=body, headers={"X-Admin-Token": "admin-token-for-tests"}
    )

    assert missing.status_code == wrong.status_code == 401
    assert missing.json()["error"]["code"] == "invalid_admin_token"
    assert right.status_code == 201


async def test_missing_api_key_is_401_with_a_challenge(client):
    response = await client.get("/v1/payments")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "missing_api_key"
    assert response.headers["WWW-Authenticate"] == "Bearer"


async def test_non_bearer_authorization_counts_as_missing(client):
    response = await client.get("/v1/payments", headers={"Authorization": "Basic dXNlcjpwYXNz"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "missing_api_key"


@pytest.mark.parametrize("key", ["nonsense", "sk_test_" + "A" * 43])
async def test_unknown_api_key_is_401(client, key):
    response = await client.get("/v1/payments", headers={"Authorization": f"Bearer {key}"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_api_key"


async def test_key_with_the_right_prefix_but_wrong_secret_is_401(client, merchant):
    forged = merchant.api_key[:12] + "x" * (len(merchant.api_key) - 12)
    response = await client.get("/v1/payments", headers={"Authorization": f"Bearer {forged}"})
    assert response.status_code == 401


async def test_issued_key_works_alongside_the_first(client, merchant):
    response = await client.post("/v1/api-keys", headers=merchant.headers)

    assert response.status_code == 201
    second = response.json()["key"]
    assert second != merchant.api_key
    for key in (merchant.api_key, second):
        listed = await client.get("/v1/payments", headers={"Authorization": f"Bearer {key}"})
        assert listed.status_code == 200


async def test_issuing_a_key_redraws_when_the_lookup_prefix_is_taken(client, merchant, monkeypatch):
    fresh = api_keys.generate_api_key()
    colliding = merchant.api_key[:12] + fresh[12:]  # same lookup prefix as the existing key
    draws = iter([colliding, fresh])
    monkeypatch.setattr(api_keys, "generate_api_key", lambda: next(draws))

    response = await client.post("/v1/api-keys", headers=merchant.headers)

    assert response.status_code == 201
    assert response.json()["key"] == fresh


async def test_revoked_key_stops_working_immediately(client, merchant):
    second = (await client.post("/v1/api-keys", headers=merchant.headers)).json()
    second_headers = {"Authorization": f"Bearer {second['key']}"}

    revoked = await client.delete(f"/v1/api-keys/{merchant.api_key_id}", headers=second_headers)

    assert revoked.status_code == 200
    assert revoked.json()["revoked_at"] is not None
    old_key = await client.get("/v1/payments", headers=merchant.headers)
    assert old_key.status_code == 401
    assert old_key.json()["error"]["code"] == "invalid_api_key"
    assert (await client.get("/v1/payments", headers=second_headers)).status_code == 200


async def test_revoking_twice_keeps_the_first_timestamp(client, merchant):
    second_id = (await client.post("/v1/api-keys", headers=merchant.headers)).json()["id"]

    first = await client.delete(f"/v1/api-keys/{second_id}", headers=merchant.headers)
    again = await client.delete(f"/v1/api-keys/{second_id}", headers=merchant.headers)

    assert again.status_code == 200
    assert again.json()["revoked_at"] == first.json()["revoked_at"]


async def test_cannot_revoke_another_merchants_key(client, merchant, make_merchant):
    other = await make_merchant("Other Co")

    response = await client.delete(f"/v1/api-keys/{other.api_key_id}", headers=merchant.headers)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "api_key_not_found"
    assert (await client.get("/v1/payments", headers=other.headers)).status_code == 200


@pytest.mark.parametrize("key_id", ["nope", "pay_" + "0" * 32, "key_" + "0" * 32])
async def test_revoking_an_unknown_key_id_is_404(client, merchant, key_id):
    response = await client.delete(f"/v1/api-keys/{key_id}", headers=merchant.headers)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "api_key_not_found"


@pytest.mark.parametrize(
    ("webhook_url", "records", "reason"),
    [
        ("http://127.0.0.1:5432/", None, "private"),
        ("http://169.254.169.254/latest/meta-data/", None, "private"),
        ("http://[::1]/hooks", None, "private"),
        ("http://localhost/hooks", ["127.0.0.1"], "private"),
        ("https://internal.example/hooks", ["10.0.0.7"], "private"),
        ("https://nowhere.invalid/hooks", None, "could not be resolved"),
    ],
)
async def test_webhook_url_must_point_at_a_public_address(
    client, dns, webhook_url, records, reason
):
    if records is not None:
        dns[webhook_url.split("/")[2]] = records

    response = await client.post("/v1/merchants", json={"name": "Acme", "webhook_url": webhook_url})

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "invalid_webhook_url"
    assert reason in error["message"]


async def test_production_requires_https_webhook_urls(app, client, settings):
    app.state.settings = settings.model_copy(
        update={"environment": "production", "admin_token": SecretStr("a" * 16)}
    )
    headers = {"X-Admin-Token": "a" * 16}

    plain = await client.post(
        "/v1/merchants",
        json={"name": "Acme", "webhook_url": "http://shop.example/hooks"},
        headers=headers,
    )
    secure = await client.post(
        "/v1/merchants",
        json={"name": "Acme", "webhook_url": "https://shop.example/hooks"},
        headers=headers,
    )

    assert plain.status_code == 422
    assert "https" in plain.json()["error"]["message"]
    assert secure.status_code == 201


async def test_private_webhook_urls_can_be_allowed_for_local_development(app, client, settings):
    app.state.settings = settings.model_copy(update={"webhook_allow_private_urls": True})

    response = await client.post(
        "/v1/merchants", json={"name": "Acme", "webhook_url": "http://127.0.0.1:9000/hooks"}
    )

    assert response.status_code == 201
