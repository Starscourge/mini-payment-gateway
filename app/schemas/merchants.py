from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, StringConstraints

from app.schemas.common import ApiKeyId, MerchantId


class CreateMerchantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    webhook_url: HttpUrl | None = None


class ApiKeyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: ApiKeyId
    prefix: str
    created_at: datetime
    revoked_at: datetime | None


class ApiKeyCreated(ApiKeyOut):
    key: str = Field(description="The secret key. Shown only once: store it securely.")


class MerchantCreated(BaseModel):
    id: MerchantId
    name: str
    webhook_url: str | None
    webhook_secret: str = Field(description="Verifies webhook signatures. Shown only once.")
    created_at: datetime
    api_key: ApiKeyCreated
