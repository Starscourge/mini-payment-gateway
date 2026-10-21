"""HMAC request signing, shared by bank callbacks and (in milestone 4) merchant webhooks.

The sender puts ``X-Signature: t=<unix time>,v1=<hex>`` on the request, where v1 is the
HMAC-SHA256 of ``"{t}.{raw body}"`` keyed with a shared secret. Signing the timestamp along
with the body means a captured request can't be replayed later with a fresh timestamp, and the
receiver rejects timestamps more than five minutes from its own clock.
"""

import hashlib
import hmac
import re
import secrets
import time

SIGNATURE_HEADER = "X-Signature"
DEFAULT_TOLERANCE_SECONDS = 300

_HEX_DIGEST = re.compile(r"[0-9a-f]{64}")


def generate_webhook_secret() -> str:
    return "whsec_" + secrets.token_urlsafe(32)


def compute_signature(secret: str, timestamp: int, body: bytes) -> str:
    # Sign the raw bytes, never a re-serialised body: JSON key order or whitespace can differ.
    message = f"{timestamp}.".encode() + body
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def signature_header(secret: str, body: bytes, *, timestamp: int | None = None) -> str:
    signed_at = int(time.time()) if timestamp is None else timestamp
    return f"t={signed_at},v1={compute_signature(secret, signed_at, body)}"


def verify_signature(
    secret: str,
    body: bytes,
    header: str,
    *,
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
    now: int | None = None,
) -> bool:
    """True if `header` is a valid, fresh signature of `body` under `secret`.

    Merchants would run exactly this to check webhooks from the gateway.
    """
    parsed = _parse_header(header)
    if parsed is None:
        return False
    signed_at, provided = parsed
    current = int(time.time()) if now is None else now
    if abs(current - signed_at) > tolerance_seconds:
        return False  # stale, or from a badly skewed clock
    # Constant-time comparison, so timing reveals nothing about how much of the digest matched.
    return hmac.compare_digest(compute_signature(secret, signed_at, body), provided)


def _parse_header(header: str) -> tuple[int, str] | None:
    fields: dict[str, str] = {}
    for part in header.split(","):
        name, separator, value = part.strip().partition("=")
        if not separator or name in fields:
            return None
        fields[name] = value
    timestamp, digest = fields.get("t", ""), fields.get("v1", "")
    if not timestamp.isdigit() or not _HEX_DIGEST.fullmatch(digest):
        return None
    return int(timestamp), digest
