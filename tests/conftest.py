"""Shared fixtures.

Integration tests need real Postgres and Redis: `docker compose up -d postgres redis`.
The test database is rebuilt from the Alembic migrations once per run, and each test runs
inside a transaction that is rolled back afterwards, so tests never see each other's data.
Unit tests use none of these fixtures and run without Docker.
"""

import ipaddress
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from pydantic import SecretStr
from sqlalchemy import Connection, func, make_url, select, text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.api.deps import get_session
from app.config import Settings
from app.main import create_app
from app.models import Base, LedgerEntry
from app.security import ssrf
from tests.helpers import (
    PUBLIC_IP,
    MerchantAccount,
    create_merchant,
    create_payment,
    post_bank_callback,
)

ROOT = Path(__file__).resolve().parent.parent
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://gateway:gateway@localhost:5432/gateway_test"
)
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")
BANK_CALLBACK_SECRET = "test-bank-callback-secret"


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings(
        _env_file=None,  # a developer's .env must not change test behaviour
        environment="test",
        database_url=TEST_DATABASE_URL,
        redis_url=TEST_REDIS_URL,
        admin_token=None,
        bank_callback_secret=SecretStr(BANK_CALLBACK_SECRET),
        log_level="WARNING",
        log_format="console",
    )


@pytest.fixture(scope="session")
def bank_secret() -> str:
    return BANK_CALLBACK_SECRET


@pytest.fixture(autouse=True)
def dns(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    """Fake DNS for the SSRF guard, so no test depends on real lookups.

    Hostnames ending in .example resolve to PUBLIC_IP; IP literals resolve to themselves; any
    other name fails to resolve. Tests can add or change records in the returned dict, e.g. to
    point a merchant's hostname at a private address after signup (DNS rebinding).
    """
    records: dict[str, list[str]] = {}

    async def resolve(host: str, port: int) -> list[str]:
        if host in records:
            return records[host]
        if host.endswith(".example"):
            return [PUBLIC_IP]
        try:
            return [str(ipaddress.ip_address(host))]
        except ValueError:
            raise ssrf.UnsafeUrl(f"the host {host!r} could not be resolved") from None

    monkeypatch.setattr(ssrf, "resolve", resolve)
    return records


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
async def committed_sessions(
    engine: AsyncEngine,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Sessions that really commit, for tests where separate connections must see each
    other's writes (row locks, SKIP LOCKED). Every table is emptied afterwards."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
        async with factory() as session:
            await _assert_ledger_balanced(session)
    finally:
        tables = ", ".join(table.name for table in Base.metadata.sorted_tables)
        async with engine.begin() as connection:
            await connection.execute(text(f"TRUNCATE {tables} RESTART IDENTITY"))


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


MerchantFactory = Callable[..., Awaitable[MerchantAccount]]


@pytest.fixture
def make_merchant(client: AsyncClient) -> MerchantFactory:
    async def make(name: str = "Acme Stores", **fields: Any) -> MerchantAccount:
        return await create_merchant(client, name, **fields)

    return make


@pytest.fixture
async def merchant(make_merchant: MerchantFactory) -> MerchantAccount:
    return await make_merchant()


PaymentFactory = Callable[..., Awaitable[dict[str, Any]]]


@pytest.fixture
def make_payment(client: AsyncClient, merchant: MerchantAccount) -> PaymentFactory:
    """Create a payment through the API (for `merchant` unless `owner` is given), optionally
    confirming it too."""

    async def make(
        owner: MerchantAccount | None = None, *, confirm: bool = False, **fields: Any
    ) -> dict[str, Any]:
        return await create_payment(client, owner or merchant, confirm=confirm, **fields)

    return make


CallbackSender = Callable[..., Awaitable[Response]]


@pytest.fixture
def send_bank_callback(client: AsyncClient, bank_secret: str) -> CallbackSender:
    """POST a bank callback signed the way the bank signs it. Overrides make it invalid."""

    async def send(
        payment_id: str, *args: Any, secret: str | None = None, **kwargs: Any
    ) -> Response:
        return await post_bank_callback(client, secret or bank_secret, payment_id, *args, **kwargs)

    return send


@pytest.fixture
async def live_client(
    settings: Settings, committed_sessions: async_sessionmaker[AsyncSession]
) -> AsyncIterator[AsyncClient]:
    """A client for an app with its own engine, so every request uses its own connection and
    really commits. For concurrency tests: requests sent at once genuinely race in Postgres.
    Depends on committed_sessions, which empties the tables afterwards."""
    app = create_app(settings)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client
    await app.state.redis.aclose()
    await app.state.engine.dispose()
