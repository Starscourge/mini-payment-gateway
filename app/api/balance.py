from fastapi import APIRouter

from app.api.deps import CurrentMerchant, SessionDep
from app.schemas.balance import BalanceOut
from app.schemas.common import error_responses
from app.services import ledger

router = APIRouter(prefix="/v1", tags=["Balance"], responses=error_responses(401))


@router.get("/balance")
async def get_balance(merchant: CurrentMerchant, session: SessionDep) -> BalanceOut:
    """Your balance, computed from the double-entry ledger rather than stored."""
    return BalanceOut(
        balance_paise=await ledger.merchant_balance(session, merchant_id=merchant.merchant_id)
    )
