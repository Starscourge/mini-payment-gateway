from datetime import datetime
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field

from app.models import OutboxEvent, OutboxStatus, WebhookDelivery


class WebhookDeliveryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    attempted_at: datetime
    http_status: int | None = Field(description="Your endpoint's status code; null if no answer.")
    duration_ms: int | None
    error: str | None = Field(description="Why no status code was received, e.g. a timeout.")


class WebhookEventOut(BaseModel):
    id: str = Field(examples=["evt_" + "0" * 32], description="Use it to drop duplicates.")
    type: str = Field(examples=["payment.succeeded"])
    status: OutboxStatus
    attempts: int
    next_attempt_at: datetime | None = Field(description="When it is next tried, while pending.")
    last_error: str | None
    created_at: datetime
    payload: dict[str, Any] = Field(description="Exactly what is POSTed to your webhook URL.")
    deliveries: list[WebhookDeliveryOut] = Field(description="Every attempt, oldest first.")

    @classmethod
    def from_event(cls, event: OutboxEvent, deliveries: list[WebhookDelivery]) -> Self:
        return cls(
            id=event.payload["id"],
            type=event.payload["type"],
            status=OutboxStatus(event.status),
            attempts=event.attempts,
            next_attempt_at=event.next_attempt_at if event.status == OutboxStatus.PENDING else None,
            last_error=event.last_error,
            created_at=event.created_at,
            payload=event.payload,
            deliveries=[WebhookDeliveryOut.model_validate(delivery) for delivery in deliveries],
        )


class WebhookEventList(BaseModel):
    data: list[WebhookEventOut]
    next_cursor: str | None = Field(
        description="Pass as `cursor` to get the next page; null on the last page."
    )
