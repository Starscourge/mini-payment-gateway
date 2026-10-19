from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, PlainSerializer, WithJsonSchema

from app import ids


def _serialize_as(prefix: str) -> PlainSerializer:
    return PlainSerializer(lambda value: ids.format_id(prefix, value), return_type=str)


def _schema_for(prefix: str) -> WithJsonSchema:
    return WithJsonSchema(
        {
            "type": "string",
            "pattern": f"^{prefix}_[0-9a-f]{{32}}$",
            "examples": [f"{prefix}_" + "0" * 32],
        }
    )


# UUIDs internally, prefixed public IDs on the wire (see app/ids.py).
MerchantId = Annotated[UUID, _serialize_as(ids.MERCHANT), _schema_for(ids.MERCHANT)]
ApiKeyId = Annotated[UUID, _serialize_as(ids.API_KEY), _schema_for(ids.API_KEY)]
PaymentId = Annotated[UUID, _serialize_as(ids.PAYMENT), _schema_for(ids.PAYMENT)]


class ErrorDetail(BaseModel):
    code: str
    message: str
    request_id: str


class ErrorResponse(BaseModel):
    error: ErrorDetail


_ERROR_DESCRIPTIONS = {
    400: "Malformed request",
    401: "Missing, unknown or revoked credentials",
    404: "Not found, or owned by another merchant",
    409: "Invalid state transition",
    422: "Validation error",
}


def error_responses(*status_codes: int) -> dict[int | str, dict[str, Any]]:
    """OpenAPI entries documenting the error shape for the given status codes."""
    return {
        code: {"model": ErrorResponse, "description": _ERROR_DESCRIPTIONS[code]}
        for code in status_codes
    }
