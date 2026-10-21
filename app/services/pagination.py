"""Opaque keyset cursors for newest-first lists.

Unlike OFFSET, a keyset cursor stays correct while new rows arrive: the next page starts
strictly after the last row the client saw, wherever that row now sits. Tables with UUID keys
page by (created_at, id), where `id` breaks ties between rows created in the same transaction
(they share a `now()` timestamp). The outbox's increasing BIGSERIAL id orders it on its own.
"""

import base64
from datetime import datetime
from uuid import UUID

from app.errors import BadRequest


def encode_cursor(created_at: datetime, row_id: UUID) -> str:
    raw = f"{created_at.isoformat()}|{row_id.hex}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        created_raw, id_raw = raw.split("|")
        created_at = datetime.fromisoformat(created_raw)
        row_id = UUID(hex=id_raw)
    except ValueError:  # also covers binascii.Error and UnicodeDecodeError
        raise _invalid_cursor() from None
    if created_at.tzinfo is None:
        raise _invalid_cursor()
    return created_at, row_id


def encode_id_cursor(row_id: int) -> str:
    return base64.urlsafe_b64encode(str(row_id).encode()).decode().rstrip("=")


def decode_id_cursor(cursor: str) -> int:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
    except ValueError:  # also covers binascii.Error and UnicodeDecodeError
        raise _invalid_cursor() from None
    if not raw.isdigit():
        raise _invalid_cursor()
    return int(raw)


def _invalid_cursor() -> BadRequest:
    return BadRequest("invalid_cursor", "The cursor is malformed. Pass next_cursor back unchanged.")
