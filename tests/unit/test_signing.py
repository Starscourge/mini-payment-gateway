import hashlib
import hmac

import pytest

from app.security.signing import compute_signature, signature_header, verify_signature

SECRET = "test-signing-secret"
BODY = b'{"payment_id":"pay_1","status":"succeeded"}'
NOW = 1_800_000_000


def test_signature_is_hmac_sha256_of_timestamp_dot_body():
    # Written out independently of the implementation: this is the documented scheme.
    expected = hmac.new(SECRET.encode(), f"{NOW}.".encode() + BODY, hashlib.sha256).hexdigest()
    assert compute_signature(SECRET, NOW, BODY) == expected
    assert signature_header(SECRET, BODY, timestamp=NOW) == f"t={NOW},v1={expected}"


def test_valid_signature_is_accepted():
    header = signature_header(SECRET, BODY, timestamp=NOW)
    assert verify_signature(SECRET, BODY, header, now=NOW)


def test_header_without_explicit_timestamp_uses_the_current_time():
    assert verify_signature(SECRET, BODY, signature_header(SECRET, BODY))


@pytest.mark.parametrize("skew", [-300, 300])
def test_timestamps_within_five_minutes_are_accepted(skew):
    header = signature_header(SECRET, BODY, timestamp=NOW + skew)
    assert verify_signature(SECRET, BODY, header, now=NOW)


@pytest.mark.parametrize("skew", [-301, 301])
def test_stale_or_future_timestamps_are_rejected(skew):
    header = signature_header(SECRET, BODY, timestamp=NOW + skew)
    assert not verify_signature(SECRET, BODY, header, now=NOW)


def test_tampered_body_is_rejected():
    header = signature_header(SECRET, BODY, timestamp=NOW)
    tampered = BODY.replace(b"succeeded", b"failed")
    assert not verify_signature(SECRET, tampered, header, now=NOW)


def test_wrong_secret_is_rejected():
    header = signature_header("some-other-secret", BODY, timestamp=NOW)
    assert not verify_signature(SECRET, BODY, header, now=NOW)


def test_timestamp_cannot_be_swapped_for_a_fresh_one():
    # A replayed request with a new t but the old v1 fails, because t is part of what's signed.
    old = signature_header(SECRET, BODY, timestamp=NOW - 3600)
    digest = old.split("v1=")[1]
    assert not verify_signature(SECRET, BODY, f"t={NOW},v1={digest}", now=NOW)


@pytest.mark.parametrize(
    "header",
    [
        "",
        "garbage",
        f"t={NOW}",
        "v1=" + "0" * 64,
        f"t=soon,v1={'0' * 64}",
        f"t={NOW},v1=xyz",
        f"t={NOW},v1={'0' * 63}",
        f"t={NOW},t={NOW},v1={'0' * 64}",
    ],
)
def test_malformed_headers_are_rejected(header):
    assert not verify_signature(SECRET, BODY, header, now=NOW)
