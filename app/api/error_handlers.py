"""Render every error in the one shape: {"error": {"code", "message", "request_id"}}.

Unhandled exceptions become 500s in RequestContextMiddleware (app/logging.py).
"""

from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.errors import AppError, error_payload

_HTTP_ERROR_CODES = {404: "not_found", 405: "method_not_allowed"}


def _error_response(
    request: Request,
    status_code: int,
    code: str,
    message: str,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    request_id: str = getattr(request.state, "request_id", "unknown")
    return JSONResponse(
        error_payload(code, message, request_id), status_code=status_code, headers=headers
    )


def _describe(error: Mapping[str, Any]) -> str:
    location = ".".join(str(part) for part in error["loc"])
    return f"{location}: {error['msg']}"


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        return _error_response(request, exc.status_code, exc.code, exc.message, exc.headers)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        errors = exc.errors()
        if any(error["type"] == "json_invalid" for error in errors):
            return _error_response(
                request, 400, "malformed_request", "The request body is not valid JSON."
            )
        message = "; ".join(_describe(error) for error in errors)
        return _error_response(request, 422, "validation_error", message)

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _HTTP_ERROR_CODES.get(exc.status_code, "http_error")
        return _error_response(request, exc.status_code, code, str(exc.detail), exc.headers)
