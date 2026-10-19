import enum
import uuid
from datetime import datetime

from sqlalchemy import CHAR, BigInteger, CheckConstraint, Enum, ForeignKey, Index, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAt, UUIDPrimaryKey


class PaymentStatus(enum.StrEnum):
    CREATED = "created"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    EXPIRED = "expired"


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint("amount_paise > 0", name="amount_positive"),
        CheckConstraint(
            "refunded_paise >= 0 AND refunded_paise <= amount_paise", name="refund_within_amount"
        ),
    )
    # Read server-generated columns back with RETURNING after INSERT and UPDATE, because an
    # async session can't lazy-load an expired updated_at later.
    # RUF012 is suppressed because SQLAlchemy types this as an instance variable, so mypy
    # rejects the ClassVar annotation ruff asks for.
    __mapper_args__ = {"eager_defaults": True}  # noqa: RUF012

    id: Mapped[UUIDPrimaryKey]
    merchant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("merchants.id"))
    amount_paise: Mapped[int] = mapped_column(BigInteger)  # money is always integer paise
    currency: Mapped[str] = mapped_column(CHAR(3), server_default="INR")
    status: Mapped[PaymentStatus] = mapped_column(
        # values_callable stores 'created', not the member name 'CREATED'
        Enum(PaymentStatus, name="payment_status", values_callable=lambda e: [m.value for m in e]),
        server_default=PaymentStatus.CREATED.value,
    )
    refunded_paise: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    description: Mapped[str | None]
    bank_reference: Mapped[str | None] = mapped_column(unique=True)
    failure_reason: Mapped[str | None]
    expires_at: Mapped[datetime]
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


# Supports newest-first cursor pagination per merchant.
Index("payments_merchant_page", Payment.merchant_id, Payment.created_at.desc(), Payment.id.desc())


class Refund(Base):
    __tablename__ = "refunds"
    __table_args__ = (CheckConstraint("amount_paise > 0", name="amount_positive"),)

    id: Mapped[UUIDPrimaryKey]
    payment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("payments.id"))
    amount_paise: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(server_default="succeeded")
    created_at: Mapped[CreatedAt]
