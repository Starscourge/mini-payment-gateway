from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import PaymentId, RefundId
from app.schemas.payments import MAX_AMOUNT_PAISE


class CreateRefundRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount_paise: (
        Annotated[
            int,
            Field(
                strict=True,
                ge=1,
                le=MAX_AMOUNT_PAISE,
                description="Amount to refund in paise. Omit it to refund everything left.",
            ),
        ]
        | None
    ) = None


class RefundOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: RefundId
    payment_id: PaymentId
    amount_paise: int
    status: str  # always 'succeeded' until refunds go through the bank (a stretch goal)
    created_at: datetime


class RefundList(BaseModel):
    data: list[RefundOut]
