"""Idempotency keys: a retried request gets the original response instead of running twice.

All of this happens in the request's own transaction:

1. INSERT (merchant_id, key, request_hash) ON CONFLICT DO NOTHING. A concurrent request with
   the same key blocks here, on the primary key, until the first commits or rolls back.
2. If the row was inserted, the handler runs and its response is saved in the row, committing
   together with the business writes. A handler that fails rolls everything back, key
   included, so a retry runs afresh: a failed request had no effect that needs protecting.
3. If the key already existed, the same request replays the stored response, and a different
   request with the same key is rejected with 422.
"""

import hashlib
import json
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, func, select, text, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import Conflict, UnprocessableEntity
from app.models import IdempotencyKey

LOCK_TIMEOUT_MS = 5000  # how long a duplicate waits for the original request to finish
RETENTION = timedelta(hours=24)  # keys older than this are purged by the worker
PURGE_BATCH = 1000
_LOCK_NOT_AVAILABLE = "55P03"  # Postgres SQLSTATE raised when lock_timeout expires


class IdempotentReplay(Exception):
    """Raised to answer with the stored response instead of running the handler again."""

    def __init__(self, status_code: int, body: dict[str, Any]) -> None:
        super().__init__(f"replaying a stored {status_code} response")
        self.status_code = status_code
        self.body = body


def request_hash(method: str, path: str, body: bytes) -> str:
    """SHA-256 of the method, path and body, identifying "the same request".

    A JSON body is canonicalised first (sorted keys, no whitespace), so a client that
    re-serialises an identical request on retry still matches.
    """
    try:
        canonical = json.dumps(json.loads(body), sort_keys=True, separators=(",", ":")).encode()
    except ValueError:  # empty or not JSON: hash the bytes as they are
        canonical = body
    return hashlib.sha256(f"{method} {path}\n".encode() + canonical).hexdigest()


def _in_progress() -> Conflict:
    return Conflict(
        "idempotency_key_in_use",
        "A request with this Idempotency-Key is still being processed. Retry shortly.",
    )


async def begin(
    session: AsyncSession, *, merchant_id: uuid.UUID, key: str, request_hash: str
) -> None:
    """Claim `key` for this request, or raise: IdempotentReplay for a repeat of a completed
    request, 422 if the key was used for a different request, 409 if it is still in flight."""
    # Waiting on the key is how a duplicate learns the original's outcome, but a duplicate
    # stuck behind a slow request should get an answer rather than hold a connection.
    await session.execute(text(f"SET LOCAL lock_timeout = {int(LOCK_TIMEOUT_MS)}"))
    try:
        inserted = await session.scalar(
            insert(IdempotencyKey)
            .values(merchant_id=merchant_id, key=key, request_hash=request_hash)
            .on_conflict_do_nothing()
            .returning(IdempotencyKey.key)
        )
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) == _LOCK_NOT_AVAILABLE:
            raise _in_progress() from None
        raise
    # Row locks taken later in the request (e.g. on the payment) wait normally.
    await session.execute(text("SET LOCAL lock_timeout = DEFAULT"))
    if inserted is not None:
        return

    # The key exists and its transaction has committed. READ COMMITTED gives this statement
    # a fresh snapshot, so it sees the stored response.
    stored = await session.scalar(
        select(IdempotencyKey).where(
            IdempotencyKey.merchant_id == merchant_id, IdempotencyKey.key == key
        )
    )
    if stored is None or stored.status_code is None or stored.response_body is None:
        raise _in_progress()  # purged in between: vanishingly rare, and safe to retry
    if stored.request_hash != request_hash:
        raise UnprocessableEntity(
            "idempotency_key_reused",
            "This Idempotency-Key was already used for a different request.",
        )
    raise IdempotentReplay(stored.status_code, stored.response_body)


async def save_response(
    session: AsyncSession,
    *,
    merchant_id: uuid.UUID,
    key: str,
    status_code: int,
    body: dict[str, Any],
) -> None:
    await session.execute(
        update(IdempotencyKey)
        .where(IdempotencyKey.merchant_id == merchant_id, IdempotencyKey.key == key)
        .values(status_code=status_code, response_body=body)
        .execution_options(synchronize_session=False)
    )


async def purge_expired(session: AsyncSession, *, limit: int = PURGE_BATCH) -> int:
    """Delete up to `limit` keys older than RETENTION. Returns how many were deleted."""
    stale = (
        select(IdempotencyKey.merchant_id, IdempotencyKey.key)
        .where(IdempotencyKey.created_at < func.now() - RETENTION)
        .limit(limit)
    )
    deleted = await session.scalars(
        delete(IdempotencyKey)
        .where(tuple_(IdempotencyKey.merchant_id, IdempotencyKey.key).in_(stale))
        .returning(IdempotencyKey.key)
        .execution_options(synchronize_session=False)
    )
    return len(deleted.all())
