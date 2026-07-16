"""Normalized error envelopes (RFC 9457 problem details) for the /v1 domain API."""

import json
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException as FastAPIHTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from apex.domain.diagnostics import bounded_diagnostic, safe_type_name
from apex.domain.input_limits import safe_validation_message

logger = structlog.get_logger(__name__)

PROBLEM_MEDIA_TYPE = "application/problem+json"
MAX_VALIDATION_ERRORS = 32
MAX_VALIDATION_LOCATION_COMPONENTS = 8
MAX_VALIDATION_LOCATION_CHARS = 64
MAX_VALIDATION_MESSAGE_CHARS = 256
MAX_VALIDATION_TYPE_CHARS = 64
MAX_VALIDATION_ERRORS_JSON_CHARS = 12_000
MAX_PROBLEM_TITLE_CHARS = 1_024
MAX_PROBLEM_DETAIL_CHARS = 4_096
MAX_ROUTE_TEMPLATE_CHARS = 512
_VALIDATION_LOCATION_SOURCES = frozenset({"body", "cookie", "header", "path", "query"})
_HTTP_METHODS = frozenset(
    {"CONNECT", "DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT", "TRACE"}
)
_MAX_RETRY_AFTER_SECONDS = 86_400
_MAX_HTTP_EXCEPTION_HEADERS = 16
_MAX_HTTP_EXCEPTION_HEADER_NAME_CHARS = 32
_MAX_HTTP_EXCEPTION_HEADER_VALUE_CHARS = 256


def _safe_route_template(request: Request) -> str:
    """Return server-owned route metadata without logging caller path segments."""

    route = request.scope.get("route")
    template = getattr(route, "path", None)
    if (
        isinstance(template, str)
        and template.startswith("/")
        and len(template) <= MAX_ROUTE_TEMPLATE_CHARS
    ):
        return template
    return "<unmatched-route>"


def _sanitized_error_location(raw: Any, *, error_type: str) -> list[str | int]:
    if not isinstance(raw, (list, tuple)):
        return []
    components = raw[:MAX_VALIDATION_LOCATION_COMPONENTS]
    location: list[str | int] = []
    for index, component in enumerate(components):
        if isinstance(component, int) and not isinstance(component, bool):
            location.append(component if abs(component) <= 1_000_000_000 else 0)
        elif index == 0 and component in _VALIDATION_LOCATION_SOURCES:
            location.append(component)
        else:
            # Pydantic locations for mapping values contain the caller's key.
            # A route cannot distinguish those from model field names, so all
            # non-source strings fail closed instead of reflecting either.
            location.append("<field>")
    # For extra-forbid errors the final location component is the caller's
    # unknown object key, not a schema-owned field name. Never reflect it.
    if error_type == "extra_forbidden" and location:
        location[-1] = "<unknown-field>"
    return location


def _sanitized_validation_errors(exc: RequestValidationError) -> list[dict[str, Any]]:
    """Return useful validation metadata without reflecting request values.

    Pydantic's default error dictionaries include the rejected ``input`` and a
    ``ctx`` mapping that can contain arbitrary values (including exception
    objects).  Request bodies may contain credentials even when the endpoint
    rejects them, so only the stable diagnostic fields are safe for a client
    response.
    """

    sanitized: list[dict[str, Any]] = []
    encoded_chars = 2  # surrounding JSON list brackets
    raw_errors = exc.errors()
    for error in raw_errors[:MAX_VALIDATION_ERRORS]:
        if not isinstance(error, dict):
            continue
        error_type = bounded_diagnostic(
            error.get("type", "validation_error"),
            max_chars=MAX_VALIDATION_TYPE_CHARS,
        )
        candidate = {
            "type": error_type,
            "loc": _sanitized_error_location(error.get("loc", ()), error_type=error_type),
            "msg": safe_validation_message(error_type)[:MAX_VALIDATION_MESSAGE_CHARS],
        }
        candidate_chars = len(json.dumps(candidate, ensure_ascii=True, separators=(",", ":"))) + (
            1 if sanitized else 0
        )
        if encoded_chars + candidate_chars > MAX_VALIDATION_ERRORS_JSON_CHARS:
            break
        sanitized.append(candidate)
        encoded_chars += candidate_chars
    return sanitized


def problem(
    status: int,
    title: str,
    detail: str | None = None,
    extra: dict[str, Any] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": "about:blank",
        "title": bounded_diagnostic(title, max_chars=MAX_PROBLEM_TITLE_CHARS),
        "status": status,
    }
    if detail is not None:
        body["detail"] = bounded_diagnostic(detail, max_chars=MAX_PROBLEM_DETAIL_CHARS)
    if extra:
        body.update(extra)
    return JSONResponse(body, status_code=status, media_type=PROBLEM_MEDIA_TYPE)


def _sanitized_http_exception_headers(
    raw_headers: Any,
    *,
    status_code: int,
) -> dict[str, str]:
    """Copy only response-control headers with a narrow server-owned grammar.

    ``HTTPException.headers`` is an arbitrary mapping.  Forwarding it wholesale
    can expose provider credentials/cookies or let a malformed value break the
    error response itself.  APEX exceptions currently need only numeric retry
    hints; Starlette's router additionally supplies ``Allow`` for 405 responses.
    """

    if type(raw_headers) is not dict or len(raw_headers) > _MAX_HTTP_EXCEPTION_HEADERS:
        return {}
    sanitized: dict[str, str] = {}
    for raw_name, raw_value in raw_headers.items():
        if type(raw_name) is not str or type(raw_value) is not str:
            continue
        if (
            not 1 <= len(raw_name) <= _MAX_HTTP_EXCEPTION_HEADER_NAME_CHARS
            or len(raw_value) > _MAX_HTTP_EXCEPTION_HEADER_VALUE_CHARS
        ):
            continue
        name = raw_name.casefold()
        if name == "retry-after":
            if (
                1 <= len(raw_value) <= 5
                and raw_value.isascii()
                and raw_value.isdecimal()
                and 0 <= int(raw_value) <= _MAX_RETRY_AFTER_SECONDS
            ):
                sanitized["Retry-After"] = str(int(raw_value))
            continue
        if name != "allow" or status_code != 405:
            continue
        methods = [part.strip().upper() for part in raw_value.split(",")]
        if (
            methods
            and len(methods) <= len(_HTTP_METHODS)
            and len(methods) == len(set(methods))
            and all(method in _HTTP_METHODS for method in methods)
        ):
            sanitized["Allow"] = ", ".join(methods)
    return sanitized


def _safe_http_exception_response(exc: StarletteHTTPException) -> JSONResponse:
    """Normalize a native HTTP exception without invoking hostile protocols."""

    # An arbitrary subclass can override attribute access.  Only the two native
    # concrete exception types are safe to inspect at this global boundary.
    exception_type = type(exc)
    if exception_type is not StarletteHTTPException and exception_type is not FastAPIHTTPException:
        return problem(500, "Internal server error")
    status_code = exc.status_code
    if type(status_code) is not int or not 400 <= status_code <= 599:
        return problem(500, "Internal server error")
    detail = exc.detail
    title = detail if type(detail) is str else "Request failed"
    response = problem(status_code, title)
    for name, value in _sanitized_http_exception_headers(
        exc.headers,
        status_code=status_code,
    ).items():
        response.headers[name] = value
    return response


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return _safe_http_exception_response(exc)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return problem(
            422,
            "Request validation failed",
            extra={"errors": _sanitized_validation_errors(exc)},
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        # Exception messages and tracebacks may contain provider payloads,
        # database values, or credentials. Keep production telemetry useful
        # without reflecting those unbounded values into logs.
        logger.error(
            "apex.unhandled_error",
            route=_safe_route_template(request),
            method=request.method,
            error_type=safe_type_name(exc),
        )
        return problem(500, "Internal server error")
