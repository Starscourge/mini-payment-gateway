"""Transactional outbox: every side effect leaves the system through here.

A state change and its outbox row commit together; the worker performs the HTTP call later.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, Index, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAt


class OutboxEvent(Base):
    __tablename__ = "outbox"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str]  # 'bank_submit' or 'webhook'
    merchant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("merchants.id"))
    event_type: Mapped[str | None]  # 'payment.succeeded', 'refund.succeeded', ...
    payload: Mapped[dict[str, Any]]
    status: Mapped[str] = mapped_column(server_default="pending")  # pending | done | dead
    attempts: Mapped[int] = mapped_column(server_default=text("0"))
    next_attempt_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_error: Mapped[str | None]
    created_at: Mapped[CreatedAt]


# Partial index: the worker only ever scans pending rows that are due.
Index("outbox_due", OutboxEvent.next_attempt_at, postgresql_where=OutboxEvent.status == "pending")


class WebhookDelivery(Base):
    """One row per delivery attempt, for the delivery log."""

    __tablename__ = "webhook_deliveries"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    outbox_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("outbox.id"))
    http_status: Mapped[int | None]
    duration_ms: Mapped[int | None]
    error: Mapped[str | None]
    attempted_at: Mapped[datetime] = mapped_column(server_default=func.now())
