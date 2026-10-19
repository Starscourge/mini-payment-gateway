"""API key generation and verification.

Keys look like ``sk_test_`` followed by 43 URL-safe characters (32 random bytes), and only a
SHA-256 hash is stored. A fast hash is right here, unlike for passwords: a key has 256 bits of
entropy, so there is nothing to brute-force, and every request pays the hashing cost. The
first 12 characters are stored in clear as a lookup prefix.
"""

import hashlib
import hmac
import secrets

KEY_PREFIX = "sk_test_"
LOOKUP_PREFIX_LENGTH = 12
_MAX_KEY_LENGTH = 128  # rejects absurd Authorization headers before any work is done


def generate_api_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


def lookup_prefix(key: str) -> str:
    return key[:LOOKUP_PREFIX_LENGTH]


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def key_matches(key: str, key_hash: str) -> bool:
    # Constant-time comparison: response timing reveals nothing about how much matched.
    return hmac.compare_digest(hash_api_key(key), key_hash)


def looks_like_api_key(value: str) -> bool:
    return value.startswith(KEY_PREFIX) and len(value) <= _MAX_KEY_LENGTH
