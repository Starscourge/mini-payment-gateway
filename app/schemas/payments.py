from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.models import PaymentStatus
from app.schemas.common import PaymentId

MAX_AMOUNT_PAISE = 10_000_000  # Rs 1 lakh


class CreatePaymentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # strict: JSON 500.0 or "500" is rejected rather than quietly coerced into money.
    amount_paise: Annotated[
        int,
        Field(strict=True, ge=1, le=MAX_AMOUNT_PAISE, description="Amount in paise (100 = Rs 1)."),
    ]
    currency: Literal["INR"] = "INR"
    description: Annotated[str, StringConstraints(max_length=255)] | None = None


class PaymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: PaymentId
    amount_paise: int
    currency: str
    status: PaymentStatus
    refunded_paise: int
    description: str | None
    failure_reason: str | None
    expires_at: datetime
    created_at: datetime
    updated_at: datetime


class PaymentList(BaseModel):
    data: list[PaymentOut]
    next_cursor: str | None = Field(
        description="Pass as `cursor` to get the next page; null on the last page."
    )
