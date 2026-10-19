import uuid
from datetime import datetime
from typing import Annotated, Any, ClassVar

from sqlalchemy import DateTime, MetaData, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, mapped_column

# Deterministic constraint names, so migrations can refer to (and later drop) them by name.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

UUIDPrimaryKey = Annotated[uuid.UUID, mapped_column(primary_key=True, default=uuid.uuid4)]
CreatedAt = Annotated[datetime, mapped_column(server_default=func.now())]


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map: ClassVar[dict[Any, Any]] = {
        datetime: DateTime(timezone=True),  # every timestamp is TIMESTAMPTZ
        str: Text(),
        dict[str, Any]: JSONB(),
    }
