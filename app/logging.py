"""Structured logging and the request-context middleware."""

import logging
import re
import time
import uuid

import structlog
from starlette.datastructures import MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from structlog.typing import EventDict, WrappedLogger

from app import ids
from app.errors import error_payload

log = structlog.get_logger()

# Keys whose values must never reach the logs, whatever code logs them.
_SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "api_key",
        "admin_token",
        "x_admin_token",
        "webhook_secret",
        "bank_callback_secret",
        "callback_secret",
        "password",
    }
)
# Incoming IDs are echoed into logs and headers, so accept only a safe alphabet.
_REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,128}")
_QUIET_PATHS = frozenset({"/health", "/ready"})


def _redact_secrets(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    for key in event_dict:
        if key.lower() in _SENSITIVE_KEYS:
            event_dict[key] = "[REDACTED]"
    return event_dict


def configure_logging(level: str, *, json_logs: bool) -> None:
    renderer: list[structlog.typing.Processor] = (
        [structlog.processors.dict_tracebacks, structlog.processors.JSONRenderer()]
        if json_logs
        else [structlog.dev.ConsoleRenderer()]
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _redact_secrets,
            *renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[level.upper()]
        ),
        cache_logger_on_first_use=True,
    )


class RequestContextMiddleware:
    """Gives every request an ID, binds it to the log context and returns it as X-Request-ID.

    It is also the last line of defence: an unhandled exception is logged and turned into the
    standard 500 error body, so even crashes carry a request ID. Written as plain ASGI rather
    than BaseHTTPMiddleware, which runs each request in an extra task.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _incoming_request_id(scope) or ids.format_id(ids.REQUEST, uuid.uuid4())
        scope.setdefault("state", {})["request_id"] = request_id
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        status_code = 500
        response_started = False
        started_at = time.perf_counter()

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status_code = message["status"]
                MutableHeaders(scope=message).append("X-Request-ID", request_id)
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        except Exception:
            log.exception("unhandled_exception")
            if response_started:
                raise
            response = JSONResponse(
                error_payload("internal_error", "Something went wrong on our side.", request_id),
                status_code=500,
            )
            await response(scope, receive, send_with_request_id)
        finally:
            if scope["path"] not in _QUIET_PATHS:
                log.info(
                    "request_completed",
                    method=scope["method"],
                    path=scope["path"],
                    status_code=status_code,
                    duration_ms=round((time.perf_counter() - started_at) * 1000, 1),
                )
            structlog.contextvars.clear_contextvars()


def _incoming_request_id(scope: Scope) -> str | None:
    for name, value in scope["headers"]:
        if name == b"x-request-id":
            candidate: str = value.decode("latin-1")
            return candidate if _REQUEST_ID_PATTERN.fullmatch(candidate) else None
    return None
