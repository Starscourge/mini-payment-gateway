import hashlib

import pytest

from app.security import api_keys


def test_key_format():
    key = api_keys.generate_api_key()
    assert key.startswith("sk_test_")
    assert len(key) == len("sk_test_") + 43  # 32 random bytes, base64url without padding


def test_keys_are_unique():
    assert len({api_keys.generate_api_key() for _ in range(1000)}) == 1000


def test_lookup_prefix_is_the_first_12_characters():
    key = api_keys.generate_api_key()
    assert api_keys.lookup_prefix(key) == key[:12]


def test_hash_is_sha256_of_the_full_key():
    key = api_keys.generate_api_key()
    assert api_keys.hash_api_key(key) == hashlib.sha256(key.encode()).hexdigest()


def test_key_matches_only_the_exact_key():
    key = api_keys.generate_api_key()
    stored = api_keys.hash_api_key(key)
    assert api_keys.key_matches(key, stored)
    assert not api_keys.key_matches(key[:-1], stored)
    assert not api_keys.key_matches(key + "x", stored)


@pytest.mark.parametrize("value", ["", "sk_live_abc", "Bearer sk_test_abc", "sk_test_" + "a" * 200])
def test_looks_like_api_key_rejects_junk(value):
    assert not api_keys.looks_like_api_key(value)
