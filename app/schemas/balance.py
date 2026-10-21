from typing import Literal

from pydantic import BaseModel, Field


class BalanceOut(BaseModel):
    balance_paise: int = Field(
        description="What the gateway owes you, summed from the ledger: succeeded payments "
        "minus refunds."
    )
    currency: Literal["INR"] = "INR"
