import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"

    merchant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("merchants.id"), primary_key=True)
    key: Mapped[str] = mapped_column(primary_key=True)
    request_hash: Mapped[str]  # SHA-256 of method + path + body
    # Set in the same transaction as the insert, so other transactions never see them NULL.
    status_code: Mapped[int | None]
    response_body: Mapped[dict[str, Any] | None]
    # Indexed for the worker's purge of keys older than 24 hours.
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), index=True)
