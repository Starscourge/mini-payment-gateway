import base64
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.errors import BadRequest
from app.ids import format_id, parse_id
from app.services.pagination import decode_cursor, encode_cursor


def test_public_id_round_trip():
    value = uuid4()
    public = format_id("pay", value)
    assert public == f"pay_{value.hex}"
    assert parse_id("pay", public) == value


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "pay",
        "pay_",
        "pay_not-hex",
        "pay_" + "0" * 31,
        "pay_" + "0" * 33,
        "pay_" + "A" * 32,  # only the canonical lowercase form
        "key_" + "0" * 32,  # right shape, wrong type
        "pay_12345678-1234-1234-1234-123456789abc",
    ],
)
def test_parse_id_rejects_malformed_or_mistyped_ids(raw):
    assert parse_id("pay", raw) is None


def test_cursor_round_trip():
    created_at, row_id = datetime(2026, 10, 19, 12, 0, 0, 123456, tzinfo=UTC), uuid4()
    assert decode_cursor(encode_cursor(created_at, row_id)) == (created_at, row_id)


def _b64(raw: str) -> str:
    return base64.urlsafe_b64encode(raw.encode()).decode()


@pytest.mark.parametrize(
    "cursor",
    [
        "",
        "!!!",
        _b64("not-a-cursor"),
        _b64(f"yesterday|{uuid4().hex}"),
        _b64("2026-10-19T12:00:00+00:00|nope"),
        _b64(f"2026-10-19T12:00:00|{uuid4().hex}"),  # naive timestamp
    ],
)
def test_malformed_cursor_is_a_bad_request(cursor):
    with pytest.raises(BadRequest) as exc_info:
        decode_cursor(cursor)
    assert exc_info.value.code == "invalid_cursor"
