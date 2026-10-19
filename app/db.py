from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import Settings


def create_engine(settings: Settings) -> AsyncEngine:
    # pre_ping replaces pooled connections that died, e.g. after a Postgres restart.
    return create_async_engine(settings.database_url, pool_pre_ping=True)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # Keep attributes loaded after commit: an async session can't lazy-load them again,
    # and routers serialise the objects after committing.
    return async_sessionmaker(engine, expire_on_commit=False)
