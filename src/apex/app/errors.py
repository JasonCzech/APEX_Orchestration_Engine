"""Normalized error envelopes (RFC 9457 problem details) for the /v1 domain API."""

import json
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from apex.domain.diagnostics import bounded_diagnostic
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


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        response = problem(exc.status_code, str(exc.detail))
        if exc.headers:
            response.headers.update(exc.headers)
        return response

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
            error_type=exc.__class__.__name__,
        )
        return problem(500, "Internal server error")
