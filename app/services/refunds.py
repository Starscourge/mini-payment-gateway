"""Refunds: all or part of a succeeded payment, settled immediately."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import Conflict
from app.models import PaymentStatus, Refund
from app.services import ledger, outbox
from app.services import payments as payments_service

REFUND_SUCCEEDED = "succeeded"


async def create_refund(
    session: AsyncSession,
    *,
    merchant_id: uuid.UUID,
    payment_id: uuid.UUID,
    amount_paise: int | None,
) -> Refund:
    """Refund `amount_paise`, or everything still refundable if it is None.

    The payment's row lock makes concurrent refunds take turns, so each one checks the amount
    left after the previous one committed. Without it, two refunds could both read the same
    refunded_paise and together refund more than was paid (the CHECK constraint on
    refunded_paise would then reject the second, as a backstop).
    """
    payment = await payments_service.get_payment(
        session, merchant_id=merchant_id, payment_id=payment_id, for_update=True
    )
    if payment.status != PaymentStatus.SUCCEEDED:
        raise Conflict(
            "payment_not_refundable",
            f"Only succeeded payments can be refunded; this one is {payment.status}.",
        )
    refundable = payment.amount_paise - payment.refunded_paise
    if refundable == 0:
        raise Conflict("refund_exceeds_amount", "The payment is already fully refunded.")
    amount = refundable if amount_paise is None else amount_paise
    if amount > refundable:
        raise Conflict(
            "refund_exceeds_amount",
            f"Refund of {amount} exceeds the {refundable} still refundable.",
        )

    payment.refunded_paise += amount
    refund = Refund(payment_id=payment.id, amount_paise=amount, status=REFUND_SUCCEEDED)
    session.add(refund)
    await session.flush()  # assigns refund.id, which the ledger and the event refer to
    await ledger.post_refund(session, payment, refund)
    await outbox.enqueue_refund_event(session, refund, merchant_id=merchant_id)
    return refund


async def list_refunds(
    session: AsyncSession, *, merchant_id: uuid.UUID, payment_id: uuid.UUID
) -> list[Refund]:
    """The payment's refunds, oldest first. Another merchant's payment is a 404."""
    await payments_service.get_payment(session, merchant_id=merchant_id, payment_id=payment_id)
    return list(
        await session.scalars(
            select(Refund)
            .where(Refund.payment_id == payment_id)
            .order_by(Refund.created_at, Refund.id)
        )
    )
