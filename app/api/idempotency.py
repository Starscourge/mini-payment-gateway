"""The Idempotency-Key header, for POST endpoints that must never run twice.

The dependency claims the key before the handler runs (or answers with the stored response,
see app/services/idempotency.py). The handler then finishes with `idempotency.complete(...)`,
which saves its response and commits: the idempotency layer owns the transaction, so the
stored response and the business writes commit together or not at all.
"""

import re
import uuid
from typing import Annotated

from fastapi import Depends, Header, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentMerchant, SessionDep
from app.errors import BadRequest
from app.services import idempotency as idempotency_service

_KEY_PATTERN = re.compile(r"[\x21-\x7e]{1,255}")  # printable ASCII, no spaces


class Idempotency:
    def __init__(self, session: AsyncSession, merchant_id: uuid.UUID, key: str) -> None:
        self._session = session
        self._merchant_id = merchant_id
        self._key = key

    async def complete[ResponseT: BaseModel](
        self, response: ResponseT, *, status_code: int
    ) -> ResponseT:
        """Save the response under the key and commit it with the handler's writes."""
        await idempotency_service.save_response(
            self._session,
            merchant_id=self._merchant_id,
            key=self._key,
            status_code=status_code,
            body=response.model_dump(mode="json"),
        )
        await self._session.commit()
        return response


async def claim_idempotency_key(
    request: Request,
    merchant: CurrentMerchant,
    session: SessionDep,
    idempotency_key: Annotated[
        str | None,
        Header(
            description="Any unique string, such as a UUID. Retrying with the same key "
            "returns the original response instead of repeating the request."
        ),
    ] = None,
) -> Idempotency:
    if idempotency_key is None:
        raise BadRequest(
            "idempotency_key_required",
            "Send an Idempotency-Key header (e.g. a UUID) so a retried request can't run twice.",
        )
    if not _KEY_PATTERN.fullmatch(idempotency_key):
        raise BadRequest(
            "invalid_idempotency_key", "Idempotency-Key must be 1-255 printable ASCII characters."
        )
    await idempotency_service.begin(
        session,
        merchant_id=merchant.merchant_id,
        key=idempotency_key,
        request_hash=idempotency_service.request_hash(
            request.method, request.url.path, await request.body()
        ),
    )
    return Idempotency(session, merchant.merchant_id, idempotency_key)


IdempotencyDep = Annotated[Idempotency, Depends(claim_idempotency_key)]
