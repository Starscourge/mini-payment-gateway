import asyncio
from collections.abc import Awaitable, Callable

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

router = APIRouter(tags=["Health"])
log = structlog.get_logger()

_CHECK_TIMEOUT_SECONDS = 2


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness: the process is up and serving requests."""
    return {"status": "ok"}


@router.get("/ready", responses={503: {"description": "Postgres or Redis is unavailable"}})
async def ready(request: Request) -> JSONResponse:
    """Readiness: Postgres and Redis both answer, so the instance can take traffic."""
    engine: AsyncEngine = request.app.state.engine
    redis: Redis = request.app.state.redis

    async def ping_postgres() -> None:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def ping_redis() -> None:
        await redis.ping()

    checks = {
        "postgres": await _check("postgres", ping_postgres),
        "redis": await _check("redis", ping_redis),
    }
    healthy = all(result == "ok" for result in checks.values())
    return JSONResponse(
        {"status": "ready" if healthy else "unavailable", "checks": checks},
        status_code=200 if healthy else 503,
    )


async def _check(name: str, probe: Callable[[], Awaitable[None]]) -> str:
    try:
        async with asyncio.timeout(_CHECK_TIMEOUT_SECONDS):
            await probe()
    except Exception as exc:
        log.warning("readiness_check_failed", dependency=name, error=repr(exc))
        return "unavailable"
    return "ok"
