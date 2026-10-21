"""Plain helpers shared by test modules. Fixtures live in conftest.py."""

from uuid import UUID

from app.ids import parse_id


def to_uuid(public_id: str) -> UUID:
    """The UUID inside a public ID such as ``pay_8c1f...``, for calling services directly."""
    value = parse_id(public_id.partition("_")[0], public_id)
    assert value is not None, f"not a public ID: {public_id!r}"
    return value
