from fastapi import APIRouter

from app.api.deps import CurrentMerchant, PaymentPathId, SessionDep
from app.api.idempotency import IdempotencyDep
from app.api.rate_limit import RateLimited
from app.schemas.common import error_responses
from app.schemas.refunds import CreateRefundRequest, RefundList, RefundOut
from app.services import refunds as refunds_service

router = APIRouter(
    prefix="/v1/payments/{payment_id}/refunds",
    tags=["Refunds"],
    dependencies=[RateLimited],
    responses=error_responses(401, 404, 429),
)


@router.post("", status_code=201, responses=error_responses(400, 409, 422))
async def create_refund(
    merchant: CurrentMerchant,
    payment_id: PaymentPathId,
    session: SessionDep,
    idempotency: IdempotencyDep,
    body: CreateRefundRequest | None = None,
) -> RefundOut:
    """Refund all or part of a succeeded payment.

    Omit `amount_paise` (or the whole body) to refund everything still refundable. A payment
    can be refunded several times, up to its amount. Refunds settle immediately.
    """
    refund = await refunds_service.create_refund(
        session,
        merchant_id=merchant.merchant_id,
        payment_id=payment_id,
        amount_paise=body.amount_paise if body is not None else None,
    )
    return await idempotency.complete(RefundOut.model_validate(refund), status_code=201)


@router.get("")
async def list_refunds(
    merchant: CurrentMerchant, payment_id: PaymentPathId, session: SessionDep
) -> RefundList:
    """The payment's refunds, oldest first."""
    refunds = await refunds_service.list_refunds(
        session, merchant_id=merchant.merchant_id, payment_id=payment_id
    )
    return RefundList(data=[RefundOut.model_validate(refund) for refund in refunds])
