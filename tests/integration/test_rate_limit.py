import pytest
from redis.asyncio import Redis

from app.api import rate_limit

WINDOW_START = 1_800_000_000.0  # a multiple of 60, so it begins a window


@pytest.fixture
def clock(monkeypatch):
    """Pin the limiter's clock 30 s into a window, so a real minute boundary can't split a test.
    Tests move it by changing now[0]."""
    now = [WINDOW_START + 30]
    monkeypatch.setattr(rate_limit, "clock", lambda: now[0])
    return now


async def hit(client, merchant, times):
    return [await client.get("/v1/balance", headers=merchant.headers) for _ in range(times)]


async def test_101st_request_in_a_minute_is_429(client, merchant, settings, clock):
    assert settings.rate_limit_per_minute == 100

    allowed = await hit(client, merchant, 100)
    refused = await client.get("/v1/balance", headers=merchant.headers)

    assert {response.status_code for response in allowed} == {200}
    assert allowed[0].headers["X-RateLimit-Remaining"] == "99"
    assert allowed[-1].headers["X-RateLimit-Remaining"] == "0"
    assert refused.status_code == 429
    assert refused.headers["Retry-After"] == "30"  # until the window ends
    assert refused.json()["error"]["code"] == "rate_limited"
    assert refused.headers["X-Request-ID"] == refused.json()["error"]["request_id"]


async def test_limit_resets_in_the_next_minute(client, merchant, clock):
    await hit(client, merchant, 101)

    clock[0] = WINDOW_START + 60

    assert (await client.get("/v1/balance", headers=merchant.headers)).status_code == 200


async def test_each_api_key_has_its_own_limit(client, merchant, make_merchant, clock):
    other = await make_merchant("Other Co")
    await hit(client, merchant, 101)

    assert (await client.get("/v1/balance", headers=other.headers)).status_code == 200


async def test_every_merchant_endpoint_is_limited(client, merchant, clock):
    await hit(client, merchant, 100)

    for method, path in [
        ("GET", "/v1/payments"),
        ("POST", "/v1/payments"),
        ("GET", "/v1/webhook-events"),
        ("POST", "/v1/api-keys"),
    ]:
        response = await client.request(method, path, json={}, headers=merchant.headers)
        assert response.status_code == 429, path


async def test_limiter_lets_requests_through_when_redis_is_down(app, client, merchant, clock):
    await app.state.redis.aclose()
    app.state.redis = Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2)

    response = await client.get("/v1/balance", headers=merchant.headers)

    assert response.status_code == 200
    assert "X-RateLimit-Remaining" not in response.headers
