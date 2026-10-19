import uuid
from typing import Any

from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAt


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"

    merchant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("merchants.id"), primary_key=True)
    key: Mapped[str] = mapped_column(primary_key=True)
    request_hash: Mapped[str]  # SHA-256 of method + path + body
    status_code: Mapped[int | None]
    response_body: Mapped[dict[str, Any] | None]
    created_at: Mapped[CreatedAt]
