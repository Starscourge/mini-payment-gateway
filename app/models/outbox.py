"""Transactional outbox: every side effect leaves the system through here.

A state change and its outbox row commit together; the worker performs the HTTP call later.
"""

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, Index, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAt


class OutboxKind(enum.StrEnum):
    BANK_SUBMIT = "bank_submit"
    WEBHOOK = "webhook"


class OutboxStatus(enum.StrEnum):
    PENDING = "pending"
    DONE = "done"
    DEAD = "dead"  # gave up after too many failed attempts


class OutboxEvent(Base):
    __tablename__ = "outbox"
    # Server-set columns (created_at, next_attempt_at) are shown in the delivery log; read them
    # back on insert (see Payment).
    __mapper_args__ = {"eager_defaults": True}  # noqa: RUF012

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str]  # an OutboxKind
    merchant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("merchants.id"))
    event_type: Mapped[str | None]  # 'payment.succeeded', 'refund.succeeded', ...
    payload: Mapped[dict[str, Any]]
    status: Mapped[str] = mapped_column(server_default="pending")  # an OutboxStatus
    attempts: Mapped[int] = mapped_column(server_default=text("0"))
    next_attempt_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_error: Mapped[str | None]
    created_at: Mapped[CreatedAt]


# Partial index: the worker only ever scans pending rows that are due.
Index("outbox_due", OutboxEvent.next_attempt_at, postgresql_where=OutboxEvent.status == "pending")
# A merchant's webhook events, newest first (GET /v1/webhook-events).
Index(
    "outbox_webhook_events",
    OutboxEvent.merchant_id,
    OutboxEvent.id.desc(),
    postgresql_where=OutboxEvent.kind == "webhook",
)
# A webhook event's public ID (evt_...) lives in its payload, because that is what merchants
# receive; this expression index makes it addressable for redelivery, and guarantees it unique.
Index(
    "outbox_event_id",
    OutboxEvent.payload["id"].astext,
    unique=True,
    postgresql_where=OutboxEvent.kind == "webhook",
)


class WebhookDelivery(Base):
    """One row per delivery attempt, for the delivery log."""

    __tablename__ = "webhook_deliveries"
    __mapper_args__ = {"eager_defaults": True}  # noqa: RUF012

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Indexed for listing an event's attempts.
    outbox_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("outbox.id"), index=True)
    http_status: Mapped[int | None]
    duration_ms: Mapped[int | None]
    error: Mapped[str | None]
    attempted_at: Mapped[datetime] = mapped_column(server_default=func.now())
