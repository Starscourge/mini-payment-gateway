from app.services.idempotency import request_hash

PATH = "/v1/payments"


def test_reformatted_json_is_the_same_request():
    compact = b'{"amount_paise":500,"description":"Order 1"}'
    spaced = b'{ "description": "Order 1",\n  "amount_paise": 500 }'
    assert request_hash("POST", PATH, compact) == request_hash("POST", PATH, spaced)


def test_different_values_are_different_requests():
    assert request_hash("POST", PATH, b'{"amount_paise":500}') != request_hash(
        "POST", PATH, b'{"amount_paise":501}'
    )


def test_method_and_path_are_part_of_the_request():
    body = b"{}"
    hashes = {
        request_hash("POST", "/v1/payments", body),
        request_hash("PUT", "/v1/payments", body),
        request_hash("POST", "/v1/payments/pay_1/confirm", body),
    }
    assert len(hashes) == 3


def test_non_json_bodies_are_hashed_as_bytes():
    assert request_hash("POST", PATH, b"") != request_hash("POST", PATH, b"not json")
    assert request_hash("POST", PATH, b"\xff\xfe") == request_hash("POST", PATH, b"\xff\xfe")
