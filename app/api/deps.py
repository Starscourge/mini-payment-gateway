"""FastAPI dependencies: database session, settings, authentication and path IDs."""

import hmac
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated

import structlog
from fastapi import Depends, Request
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import ids
from app.config import Settings
from app.errors import PaymentNotFound, Unauthorized
from app.services import merchants as merchants_service


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """One session per request. Uncommitted work is rolled back when the request ends."""
    session_factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with session_factory() as session:
        yield session


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]

# auto_error=False: return None instead of FastAPI's own error, so failures use our error shape.
_bearer = HTTPBearer(auto_error=False, description="Your secret API key: `sk_test_...`")
_admin_token = APIKeyHeader(
    name="X-Admin-Token",
    auto_error=False,
    description="Required for merchant creation when the server sets ADMIN_TOKEN.",
)
_CHALLENGE = {"WWW-Authenticate": "Bearer"}


@dataclass(frozen=True)
class AuthenticatedMerchant:
    merchant_id: uuid.UUID
    api_key_id: uuid.UUID
    key_prefix: str


async def authenticate(
    session: SessionDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> AuthenticatedMerchant:
    if credentials is None:
        raise Unauthorized(
            "missing_api_key",
            "Send your API key as 'Authorization: Bearer sk_test_...'.",
            headers=_CHALLENGE,
        )
    key = await merchants_service.authenticate_api_key(session, credentials.credentials)
    if key is None:
        raise Unauthorized(
            "invalid_api_key", "The API key is unknown or has been revoked.", headers=_CHALLENGE
        )
    structlog.contextvars.bind_contextvars(merchant_id=ids.format_id(ids.MERCHANT, key.merchant_id))
    return AuthenticatedMerchant(
        merchant_id=key.merchant_id, api_key_id=key.id, key_prefix=key.prefix
    )


CurrentMerchant = Annotated[AuthenticatedMerchant, Depends(authenticate)]


def _payment_id_from_path(payment_id: str) -> uuid.UUID:
    # A malformed ID gets the same 404 as an unknown one.
    parsed = ids.parse_id(ids.PAYMENT, payment_id)
    if parsed is None:
        raise PaymentNotFound()
    return parsed


# The {payment_id} path parameter, parsed. Declare it after CurrentMerchant, so a request
# without credentials gets 401 rather than 404.
PaymentPathId = Annotated[uuid.UUID, Depends(_payment_id_from_path)]


async def require_admin(
    settings: SettingsDep, token: Annotated[str | None, Depends(_admin_token)]
) -> None:
    expected = settings.admin_token
    if expected is None:
        return  # open: Settings only allows this outside production
    if token is None or not hmac.compare_digest(
        token.encode(), expected.get_secret_value().encode()
    ):
        raise Unauthorized("invalid_admin_token", "A valid X-Admin-Token header is required.")
