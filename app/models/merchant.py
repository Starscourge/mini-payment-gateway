import uuid
from datetime import datetime

from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAt, UUIDPrimaryKey


class Merchant(Base):
    __tablename__ = "merchants"

    id: Mapped[UUIDPrimaryKey]
    name: Mapped[str]
    webhook_url: Mapped[str | None]
    # Stored recoverably (not hashed) because it is needed to sign webhooks.
    webhook_secret: Mapped[str]
    created_at: Mapped[CreatedAt]


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[UUIDPrimaryKey]
    merchant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("merchants.id"))
    prefix: Mapped[str] = mapped_column(unique=True)  # first 12 chars of the key, for lookup
    key_hash: Mapped[str]  # SHA-256 of the full key; the key itself is never stored
    revoked_at: Mapped[datetime | None]
    created_at: Mapped[CreatedAt]
