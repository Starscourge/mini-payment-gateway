"""Messages exchanged with the bank: our submission, its reply, and its signed callback."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints

from app.models import PaymentStatus

BankReference = Annotated[str, StringConstraints(min_length=1, max_length=64)]


class BankSubmissionAccepted(BaseModel):
    """The bank's 202 reply to a submission."""

    bank_reference: BankReference


class BankCallback(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # A public ID such as pay_8c1f..., parsed by the endpoint like any path ID.
    payment_id: Annotated[str, StringConstraints(max_length=64)]
    bank_reference: BankReference
    status: Literal[PaymentStatus.SUCCEEDED, PaymentStatus.FAILED]
    failure_reason: Annotated[str, StringConstraints(max_length=255)] | None = None


class BankCallbackAck(BaseModel):
    applied: bool  # False when the callback was a repeat and changed nothing
