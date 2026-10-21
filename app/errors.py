"""Errors the API can return.

Services raise these and `app.api.error_handlers` renders them in the one error shape:
``{"error": {"code", "message", "request_id"}}``. This module imports no web framework, so
services and the worker can use it too.
"""

from typing import Any, ClassVar


class AppError(Exception):
    status_code: ClassVar[int] = 400

    def __init__(self, code: str, message: str, *, headers: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.headers = headers


class BadRequest(AppError):
    status_code = 400


class Unauthorized(AppError):
    status_code = 401


class NotFound(AppError):
    status_code = 404


class Conflict(AppError):
    status_code = 409


class UnprocessableEntity(AppError):
    status_code = 422


class PaymentNotFound(NotFound):
    def __init__(self) -> None:
        super().__init__("payment_not_found", "No payment with that ID.")


class ApiKeyNotFound(NotFound):
    def __init__(self) -> None:
        super().__init__("api_key_not_found", "No API key with that ID.")


def error_payload(code: str, message: str, request_id: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "request_id": request_id}}
