async def test_health(client):
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_ready_checks_postgres_and_redis(client):
    response = await client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {"postgres": "ok", "redis": "ok"}}


async def test_ready_reports_an_unavailable_dependency(app, client):
    await app.state.redis.aclose()
    app.state.redis = app.state.redis.from_url("redis://127.0.0.1:1/0")  # nothing listens here
    response = await client.get("/ready")
    assert response.status_code == 503
    assert response.json()["checks"] == {"postgres": "ok", "redis": "unavailable"}


async def test_responses_carry_a_generated_request_id(client):
    response = await client.get("/health")
    assert response.headers["X-Request-ID"].startswith("req_")


async def test_incoming_request_id_is_echoed(client):
    response = await client.get("/health", headers={"X-Request-ID": "trace-123"})
    assert response.headers["X-Request-ID"] == "trace-123"


async def test_unsafe_incoming_request_id_is_replaced(client):
    response = await client.get("/health", headers={"X-Request-ID": "has spaces; and=junk"})
    assert response.headers["X-Request-ID"].startswith("req_")


async def test_unknown_route_uses_the_error_shape(client):
    response = await client.get("/v1/nope")
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "not_found"
    assert error["request_id"] == response.headers["X-Request-ID"]


async def test_unhandled_exception_becomes_a_500_with_a_request_id(app, client):
    async def boom() -> None:
        raise RuntimeError("database password is hunter2")

    app.add_api_route("/boom", boom)
    response = await client.get("/boom")

    assert response.status_code == 500
    error = response.json()["error"]
    assert error["code"] == "internal_error"
    assert error["request_id"] == response.headers["X-Request-ID"]
    assert "hunter2" not in response.text  # internals never leak to the client
