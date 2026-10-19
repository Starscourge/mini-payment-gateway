"""HMAC signing secrets. Webhook signing and verification arrive in milestone 4."""

import secrets


def generate_webhook_secret() -> str:
    return "whsec_" + secrets.token_urlsafe(32)
