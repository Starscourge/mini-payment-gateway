"""Merchants and their API keys."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ApiKeyNotFound
from app.models import ApiKey, Merchant
from app.security import api_keys
from app.security.signing import generate_webhook_secret

_MAX_PREFIX_ATTEMPTS = 5


@dataclass(frozen=True)
class IssuedApiKey:
    record: ApiKey
    plaintext: str  # returned to the merchant once and never stored


async def create_merchant(
    session: AsyncSession, *, name: str, webhook_url: str | None
) -> tuple[Merchant, IssuedApiKey]:
    merchant = Merchant(
        name=name, webhook_url=webhook_url, webhook_secret=generate_webhook_secret()
    )
    session.add(merchant)
    await session.flush()  # assigns merchant.id
    return merchant, await issue_api_key(session, merchant_id=merchant.id)


async def issue_api_key(session: AsyncSession, *, merchant_id: uuid.UUID) -> IssuedApiKey:
    # The lookup prefix holds only 4 random characters (24 bits), so a collision is rare but
    # possible: draw a new key if the prefix is taken. The UNIQUE constraint is the backstop.
    for _ in range(_MAX_PREFIX_ATTEMPTS):
        plaintext = api_keys.generate_api_key()
        prefix = api_keys.lookup_prefix(plaintext)
        if await session.scalar(select(ApiKey.id).where(ApiKey.prefix == prefix)) is None:
            break
    else:
        raise RuntimeError("could not find an unused API key prefix")

    record = ApiKey(
        merchant_id=merchant_id, prefix=prefix, key_hash=api_keys.hash_api_key(plaintext)
    )
    session.add(record)
    await session.flush()
    return IssuedApiKey(record=record, plaintext=plaintext)


async def revoke_api_key(
    session: AsyncSession, *, merchant_id: uuid.UUID, key_id: uuid.UUID
) -> ApiKey:
    """Revoke a key. Revoking an already revoked key is a no-op that keeps the first timestamp."""
    record = await session.scalar(
        select(ApiKey).where(ApiKey.id == key_id, ApiKey.merchant_id == merchant_id)
    )
    if record is None:
        raise ApiKeyNotFound()
    if record.revoked_at is None:
        record.revoked_at = datetime.now(UTC)
        await session.flush()
    return record


async def authenticate_api_key(session: AsyncSession, presented: str) -> ApiKey | None:
    """Return the active key matching `presented`, or None if it is unknown or revoked."""
    if not api_keys.looks_like_api_key(presented):
        return None
    record = await session.scalar(
        select(ApiKey).where(ApiKey.prefix == api_keys.lookup_prefix(presented))
    )
    if record is None or record.revoked_at is not None:
        return None
    if not api_keys.key_matches(presented, record.key_hash):
        return None
    return record
