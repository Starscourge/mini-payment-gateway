"""Public IDs: a type prefix plus the UUID's hex, e.g. ``pay_8c1f2a9e...``.

The database stores plain UUIDs; prefixes exist only at the API boundary. They make IDs
self-describing in logs and support requests, and stop an ID of one type being accepted where
another is expected.
"""

import re
from uuid import UUID

MERCHANT = "mer"
API_KEY = "key"
PAYMENT = "pay"
REQUEST = "req"

_HEX32 = re.compile(r"[0-9a-f]{32}")


def format_id(prefix: str, value: UUID) -> str:
    return f"{prefix}_{value.hex}"


def parse_id(prefix: str, raw: str) -> UUID | None:
    """Return the UUID inside a public ID, or None if it is malformed or has another prefix."""
    head, separator, tail = raw.partition("_")
    if head != prefix or not separator or not _HEX32.fullmatch(tail):
        return None
    return UUID(hex=tail)
