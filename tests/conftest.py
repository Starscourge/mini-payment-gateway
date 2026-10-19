"""Shared fixtures.

Integration tests need real Postgres and Redis: `docker compose up -d postgres redis`.
The test database is rebuilt from the Alembic migrations once per run, and each test runs
inside a transaction that is rolled back afterwards, so tests never see each other's data.
Unit tests use none of these fixtures and run without Docker.
"""

import os
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import Connection, func, make_url, select, text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from app.api.deps import get_session
from app.config import Settings
from app.main import create_app
from app.models import LedgerEntry

ROOT = Path(__file__).resolve().parent.parent
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://gateway:gateway@localhost:5432/gateway_test"
)
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings(
        _env_file=None,  # a developer's .env must not change test behaviour
        environment="test",
        database_url=TEST_DATABASE_URL,
        redis_url=TEST_REDIS_URL,
        admin_token=None,
        log_level="WARNING",
        log_format="console",
    )


async def _create_database_if_missing(url: URL) -> None:
    admin = create_async_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as connection:
            exists = await connection.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": url.database}
            )
            if not exists:
                await connection.execute(text(f'CREATE DATABASE "{url.database}"'))
    finally:
        await admin.dispose()


def _run_migrations(connection: Connection) -> None:
    config = AlembicConfig(str(ROOT / "alembic.ini"))
    config.attributes["connection"] = connection
    config.attributes["configure_logger"] = False
    command.upgrade(config, "head")


@pytest.fixture(scope="session")
async def engine(settings: Settings) -> AsyncIterator[AsyncEngine]:
    url = make_url(settings.database_url)
    await _create_database_if_missing(url)
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        # Start from an empty schema, so every run also tests the migrations themselves.
        await connection.execute(text("DROP SCHEMA public CASCADE"))
        await connection.execute(text("CREATE SCHEMA public"))
        await connection.run_sync(_run_migrations)
    yield engine
    await engine.dispose()


async def _assert_ledger_balanced(session: AsyncSession) -> None:
    """The core money invariant: every ledger transaction's entries sum to zero."""
    unbalanced = (
        await session.execute(
            select(LedgerEntry.transaction_id, func.sum(LedgerEntry.amount_paise))
            .group_by(LedgerEntry.transaction_id)
            .having(func.sum(LedgerEntry.amount_paise) != 0)
        )
    ).all()
    assert not unbalanced, f"unbalanced ledger transactions: {unbalanced}"


@pytest.fixture
async def db_session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A session inside a transaction that is rolled back after the test.

    join_transaction_mode="create_savepoint" turns the app's session.commit() into a savepoint
    release, so code under test commits normally and still nothing persists.
    """
    async with engine.connect() as connection:
        transaction = await connection.begin()
        session = AsyncSession(
            bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False
        )
        try:
            yield session
            await _assert_ledger_balanced(session)
        finally:
            await session.close()
            await transaction.rollback()


@pytest.fixture
async def app(settings: Settings, db_session: AsyncSession) -> AsyncIterator[FastAPI]:
    app = create_app(settings)

    async def session_override() -> AsyncIterator[AsyncSession]:
        try:
            yield db_session
        except Exception:
            # Like get_session closing the session: a failed request's writes are undone.
            await db_session.rollback()
            raise

    app.dependency_overrides[get_session] = session_override
    yield app
    await app.state.redis.aclose()
    await app.state.engine.dispose()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


@dataclass(frozen=True)
class MerchantAccount:
    id: str
    api_key: str
    api_key_id: str
    webhook_secret: str

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}


MerchantFactory = Callable[..., Awaitable[MerchantAccount]]


@pytest.fixture
def make_merchant(client: AsyncClient) -> MerchantFactory:
    async def make(name: str = "Acme Stores") -> MerchantAccount:
        response = await client.post("/v1/merchants", json={"name": name})
        assert response.status_code == 201, response.text
        body = response.json()
        return MerchantAccount(
            id=body["id"],
            api_key=body["api_key"]["key"],
            api_key_id=body["api_key"]["id"],
            webhook_secret=body["webhook_secret"],
        )

    return make


@pytest.fixture
async def merchant(make_merchant: MerchantFactory) -> MerchantAccount:
    return await make_merchant()
