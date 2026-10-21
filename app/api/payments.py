from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import CurrentMerchant, PaymentPathId, SessionDep, SettingsDep
from app.api.idempotency import IdempotencyDep
from app.api.rate_limit import RateLimited
from app.models import PaymentStatus
from app.schemas.common import error_responses
from app.schemas.payments import CreatePaymentRequest, PaymentList, PaymentOut
from app.services import payments as payments_service

router = APIRouter(
    prefix="/v1/payments",
    tags=["Payments"],
    dependencies=[RateLimited],
    responses=error_responses(401, 429),
)


@router.post("", status_code=201, responses=error_responses(400, 409, 422))
async def create_payment(
    body: CreatePaymentRequest,
    merchant: CurrentMerchant,
    session: SessionDep,
    settings: SettingsDep,
    idempotency: IdempotencyDep,
) -> PaymentOut:
    """Create a payment order. It expires if not confirmed within PAYMENT_TTL_MINUTES."""
    payment = await payments_service.create_payment(
        session,
        merchant_id=merchant.merchant_id,
        amount_paise=body.amount_paise,
        currency=body.currency,
        description=body.description,
        ttl=timedelta(minutes=settings.payment_ttl_minutes),
    )
    return await idempotency.complete(PaymentOut.model_validate(payment), status_code=201)


@router.get("/{payment_id}", responses=error_responses(404))
async def get_payment(
    merchant: CurrentMerchant, payment_id: PaymentPathId, session: SessionDep
) -> PaymentOut:
    payment = await payments_service.get_payment(
        session, merchant_id=merchant.merchant_id, payment_id=payment_id
    )
    return PaymentOut.model_validate(payment)


@router.post("/{payment_id}/confirm", responses=error_responses(400, 404, 409, 422))
async def confirm_payment(
    merchant: CurrentMerchant,
    payment_id: PaymentPathId,
    session: SessionDep,
    idempotency: IdempotencyDep,
) -> PaymentOut:
    """Simulate the customer approving the payment.

    The payment becomes `processing` and is queued for submission to the bank; the bank's
    callback later makes it `succeeded` or `failed`. Fails with 409 unless the payment is
    `created` and not yet expired.
    """
    payment = await payments_service.confirm_payment(
        session, merchant_id=merchant.merchant_id, payment_id=payment_id
    )
    return await idempotency.complete(PaymentOut.model_validate(payment), status_code=200)


@router.get("", responses=error_responses(400, 422))
async def list_payments(
    merchant: CurrentMerchant,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
    status: PaymentStatus | None = None,
) -> PaymentList:
    """List payments newest first, with cursor pagination and an optional status filter."""
    payments, next_cursor = await payments_service.list_payments(
        session, merchant_id=merchant.merchant_id, limit=limit, cursor=cursor, status=status
    )
    return PaymentList(
        data=[PaymentOut.model_validate(payment) for payment in payments], next_cursor=next_cursor
    )
