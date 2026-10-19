from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Query

from app import ids
from app.api.deps import CurrentMerchant, SessionDep, SettingsDep
from app.errors import PaymentNotFound
from app.models import PaymentStatus
from app.schemas.common import error_responses
from app.schemas.payments import CreatePaymentRequest, PaymentList, PaymentOut
from app.services import payments as payments_service

router = APIRouter(prefix="/v1/payments", tags=["Payments"], responses=error_responses(401))


@router.post("", status_code=201, responses=error_responses(400, 422))
async def create_payment(
    body: CreatePaymentRequest,
    merchant: CurrentMerchant,
    session: SessionDep,
    settings: SettingsDep,
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
    await session.commit()
    return PaymentOut.model_validate(payment)


@router.get("/{payment_id}", responses=error_responses(404, 422))
async def get_payment(
    payment_id: str, merchant: CurrentMerchant, session: SessionDep
) -> PaymentOut:
    payment_uuid = ids.parse_id(ids.PAYMENT, payment_id)
    if payment_uuid is None:
        raise PaymentNotFound()
    payment = await payments_service.get_payment(
        session, merchant_id=merchant.merchant_id, payment_id=payment_uuid
    )
    return PaymentOut.model_validate(payment)


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
