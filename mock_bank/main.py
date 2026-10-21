"""A mock bank, run as its own process so that delays, failures and duplicate callbacks reach
the gateway over a real network.

``POST /payments`` accepts a payment with 202 and a ``bank_reference``. After a random delay
the bank reports the outcome to the gateway's callback URL, signed with the shared secret
(see app/security/signing.py), and retries a callback the gateway doesn't accept. It treats
``payment_id`` as an idempotency key, so a repeated submission returns the first reference and
settles nothing new. State lives in memory and is lost on restart, which is fine for a mock.

Run with: uvicorn mock_bank.main:create_app --factory --port 8001
"""

import asyncio
import json
import random
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Self

import httpx
import structlog
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.logging import configure_logging
from app.security.signing import SIGNATURE_HEADER, signature_header

log = structlog.get_logger()

FAILURE_REASONS = ("insufficient_funds", "declined_by_issuer", "risk_check_failed")


class BankSettings(BaseSettings):
    """Read from BANK_* environment variables, e.g. BANK_FAILURE_RATE=0.5."""

    # validate_by_name: log_level/log_format read the unprefixed LOG_LEVEL/LOG_FORMAT, like the
    # gateway, but can still be passed by field name.
    model_config = SettingsConfigDict(
        env_prefix="BANK_", env_file=".env", extra="ignore", validate_by_name=True
    )

    callback_url: str = "http://localhost:8000/internal/bank/callback"
    callback_secret: SecretStr = Field(min_length=16)  # same value as the gateway's
    failure_rate: float = Field(default=0.1, ge=0, le=1)  # share of payments declined
    duplicate_rate: float = Field(default=0.1, ge=0, le=1)  # share of callbacks sent twice at once
    # Share of submissions answered with 503 *after* being accepted: the reply is "lost", so
    # the gateway can't tell whether the bank got the payment, and must retry safely.
    lost_response_rate: float = Field(default=0.0, ge=0, le=1)
    min_delay_seconds: float = Field(default=0.5, ge=0)
    max_delay_seconds: float = Field(default=3.0, ge=0)
    callback_attempts: int = Field(default=5, ge=1)
    callback_retry_seconds: float = Field(default=1.0, ge=0)  # doubles after each failure
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(
        default="INFO", validation_alias="LOG_LEVEL"
    )
    log_format: Literal["json", "console"] = Field(default="json", validation_alias="LOG_FORMAT")

    @model_validator(mode="after")
    def _delays_in_order(self) -> Self:
        if self.min_delay_seconds > self.max_delay_seconds:
            raise ValueError("BANK_MIN_DELAY_SECONDS must not exceed BANK_MAX_DELAY_SECONDS")
        return self


@dataclass(frozen=True)
class Submission:
    payment_id: str
    amount_paise: int
    bank_reference: str


class Bank:
    def __init__(self, settings: BankSettings, http: httpx.AsyncClient, rng: random.Random) -> None:
        self.settings = settings
        self.rng = rng
        self.submissions: dict[str, Submission] = {}  # by payment_id
        self._http = http
        self._tasks: set[asyncio.Task[None]] = set()

    def submit(self, payment_id: str, amount_paise: int) -> Submission:
        """Accept a payment, or return the earlier submission if this payment_id was seen."""
        if (existing := self.submissions.get(payment_id)) is not None:
            log.info("duplicate_submission", payment_id=payment_id)
            return existing
        submission = Submission(payment_id, amount_paise, "bnk_" + secrets.token_hex(12))
        self.submissions[payment_id] = submission
        task = asyncio.create_task(self._settle(submission))
        self._tasks.add(task)  # keep a reference, or the task may be garbage-collected
        task.add_done_callback(self._tasks.discard)
        return submission

    async def _settle(self, submission: Submission) -> None:
        settings = self.settings
        try:
            await asyncio.sleep(
                self.rng.uniform(settings.min_delay_seconds, settings.max_delay_seconds)
            )
            failed = self.rng.random() < settings.failure_rate
            body = {
                "payment_id": submission.payment_id,
                "bank_reference": submission.bank_reference,
                "status": "failed" if failed else "succeeded",
                "failure_reason": self.rng.choice(FAILURE_REASONS) if failed else None,
            }
            copies = 2 if self.rng.random() < settings.duplicate_rate else 1
            # Duplicates go out concurrently, to exercise the gateway's row lock.
            await asyncio.gather(*(self._send_callback(body) for _ in range(copies)))
        except Exception:
            log.exception("settlement_failed", payment_id=submission.payment_id)

    async def _send_callback(self, body: dict[str, Any]) -> None:
        settings = self.settings
        raw = json.dumps(body).encode()
        delay = settings.callback_retry_seconds
        for attempt in range(1, settings.callback_attempts + 1):
            # Re-signed on every attempt, so a retry carries a fresh timestamp.
            headers = {
                "Content-Type": "application/json",
                SIGNATURE_HEADER: signature_header(
                    settings.callback_secret.get_secret_value(), raw
                ),
            }
            try:
                response = await self._http.post(
                    settings.callback_url, content=raw, headers=headers
                )
            except httpx.HTTPError as exc:
                error = repr(exc)
            else:
                if response.is_success:
                    log.info("callback_delivered", payment_id=body["payment_id"], attempt=attempt)
                    return
                error = f"HTTP {response.status_code}"
            log.warning(
                "callback_failed", payment_id=body["payment_id"], attempt=attempt, error=error
            )
            if attempt < settings.callback_attempts:
                await asyncio.sleep(delay)
                delay *= 2
        log.error("callback_abandoned", payment_id=body["payment_id"])

    async def drain(self) -> None:
        """Wait until every scheduled settlement has finished. Used by tests."""
        while self._tasks:
            await asyncio.gather(*self._tasks)

    async def aclose(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._http.aclose()


class SubmitPayment(BaseModel):
    payment_id: str = Field(pattern=r"^pay_[0-9a-f]{32}$")
    amount_paise: int = Field(strict=True, gt=0)
    currency: Literal["INR"]


def get_bank(request: Request) -> Bank:
    bank: Bank = request.app.state.bank
    return bank


BankDep = Annotated[Bank, Depends(get_bank)]
router = APIRouter()


@router.post("/payments", status_code=202)
async def submit_payment(body: SubmitPayment, bank: BankDep) -> JSONResponse:
    submission = bank.submit(body.payment_id, body.amount_paise)
    if bank.rng.random() < bank.settings.lost_response_rate:
        log.info("response_lost", payment_id=body.payment_id)
        return JSONResponse({"error": "temporarily_unavailable"}, status_code=503)
    return JSONResponse(
        {"bank_reference": submission.bank_reference, "status": "pending"}, status_code=202
    )


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


def create_app(
    settings: BankSettings | None = None,
    *,
    callback_transport: httpx.AsyncBaseTransport | None = None,
    rng: random.Random | None = None,
) -> FastAPI:
    """Tests pass zero delays, a fixed `rng` and a transport that delivers callbacks in-process."""
    settings = settings or BankSettings()
    configure_logging(settings.log_level, json_logs=settings.log_format == "json")
    bank = Bank(
        settings,
        httpx.AsyncClient(timeout=5.0, transport=callback_transport),
        rng or random.Random(),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        await bank.aclose()

    app = FastAPI(title="Mock Bank", lifespan=lifespan)
    app.state.bank = bank
    app.include_router(router)
    return app
