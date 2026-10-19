from fastapi import APIRouter, Depends

from app import ids
from app.api.deps import CurrentMerchant, SessionDep, require_admin
from app.errors import ApiKeyNotFound
from app.schemas.common import error_responses
from app.schemas.merchants import ApiKeyCreated, ApiKeyOut, CreateMerchantRequest, MerchantCreated
from app.services import merchants as merchants_service
from app.services.merchants import IssuedApiKey

router = APIRouter(prefix="/v1", tags=["Merchants and API keys"])


def _key_created(issued: IssuedApiKey) -> ApiKeyCreated:
    return ApiKeyCreated(
        id=issued.record.id,
        prefix=issued.record.prefix,
        created_at=issued.record.created_at,
        revoked_at=issued.record.revoked_at,
        key=issued.plaintext,
    )


@router.post(
    "/merchants",
    status_code=201,
    dependencies=[Depends(require_admin)],
    responses=error_responses(400, 401, 422),
)
async def create_merchant(body: CreateMerchantRequest, session: SessionDep) -> MerchantCreated:
    """Create a merchant with its first API key.

    The API key and webhook secret appear only in this response. Open in local development;
    elsewhere it needs the `X-Admin-Token` header.
    """
    merchant, issued = await merchants_service.create_merchant(
        session,
        name=body.name,
        webhook_url=str(body.webhook_url) if body.webhook_url is not None else None,
    )
    await session.commit()
    return MerchantCreated(
        id=merchant.id,
        name=merchant.name,
        webhook_url=merchant.webhook_url,
        webhook_secret=merchant.webhook_secret,
        created_at=merchant.created_at,
        api_key=_key_created(issued),
    )


@router.post("/api-keys", status_code=201, responses=error_responses(401))
async def issue_api_key(merchant: CurrentMerchant, session: SessionDep) -> ApiKeyCreated:
    """Issue an additional API key, e.g. to rotate keys without downtime."""
    issued = await merchants_service.issue_api_key(session, merchant_id=merchant.merchant_id)
    await session.commit()
    return _key_created(issued)


@router.delete("/api-keys/{key_id}", responses=error_responses(401, 404, 422))
async def revoke_api_key(key_id: str, merchant: CurrentMerchant, session: SessionDep) -> ApiKeyOut:
    """Revoke an API key. It stops working immediately; revoking twice is harmless."""
    key_uuid = ids.parse_id(ids.API_KEY, key_id)
    if key_uuid is None:
        raise ApiKeyNotFound()
    record = await merchants_service.revoke_api_key(
        session, merchant_id=merchant.merchant_id, key_id=key_uuid
    )
    await session.commit()
    return ApiKeyOut.model_validate(record)
