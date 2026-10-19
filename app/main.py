"""Application factory. Run with: uvicorn app.main:create_app --factory"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from redis.asyncio import Redis

from app.api import health, merchants, payments
from app.api.error_handlers import register_error_handlers
from app.config import Settings, get_settings
from app.db import create_engine, create_session_factory
from app.logging import RequestContextMiddleware, configure_logging


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    await app.state.redis.aclose()
    await app.state.engine.dispose()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, json_logs=settings.log_format == "json")

    app = FastAPI(
        title="Mini Payment Gateway",
        version="0.1.0",
        summary="Payment orders, settlement and refunds that stay correct under retries.",
        lifespan=lifespan,
    )
    # Connections open lazily, so building these does no I/O.
    engine = create_engine(settings)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)
    app.state.redis = Redis.from_url(settings.redis_url)

    app.add_middleware(RequestContextMiddleware)
    register_error_handlers(app)
    app.include_router(health.router)
    app.include_router(merchants.router)
    app.include_router(payments.router)
    return app
