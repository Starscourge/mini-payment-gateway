"""Settlement results from the bank. Internal: authenticated by HMAC signature, not an API key."""

from typing import Annotated

import structlog
from fastapi import APIRouter, Header, Request
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from app import ids
from app.api.deps import SessionDep, SettingsDep
from app.errors import PaymentNotFound, Unauthorized
from app.schemas.bank import BankCallback, BankCallbackAck
from app.schemas.common import error_responses
from app.security import signing
from app.services import payments as payments_service

router = APIRouter(prefix="/internal/bank", tags=["Internal"])
log = structlog.get_logger()


@router.post(
    "/callback",
    responses=error_responses(400, 401, 404, 409, 422),
    # The body is read raw (the signature covers the exact bytes), so describe it by hand.
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": BankCallback.model_json_schema()}},
        }
    },
)
async def bank_callback(
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    x_signature: Annotated[str | None, Header(description="t=<unix>,v1=<hex HMAC-SHA256>")] = None,
) -> BankCallbackAck:
    """The bank reports whether a submitted payment succeeded or failed.

    Repeats are expected and harmless: a callback for a payment that is already final returns
    200 and changes nothing, so the bank stops retrying.
    """
    body = await request.body()
    # Authenticate before parsing: nothing in an unsigned body is trusted.
    secret = settings.bank_callback_secret
    if (
        secret is None
        or x_signature is None
        or not signing.verify_signature(secret.get_secret_value(), body, x_signature)
    ):
        raise Unauthorized("invalid_signature", "Missing, invalid or expired X-Signature header.")
    try:
        callback = BankCallback.model_validate_json(body)
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from None

    payment_id = ids.parse_id(ids.PAYMENT, callback.payment_id)
    if payment_id is None:
        raise PaymentNotFound()
    structlog.contextvars.bind_contextvars(payment_id=callback.payment_id)
    applied = await payments_service.apply_bank_result(
        session,
        payment_id=payment_id,
        bank_reference=callback.bank_reference,
        outcome=callback.status,
        failure_reason=callback.failure_reason,
    )
    await session.commit()
    log.info("bank_callback_received", outcome=callback.status, applied=applied)
    return BankCallbackAck(applied=applied)
