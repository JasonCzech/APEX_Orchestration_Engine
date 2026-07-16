"""/logs: log-search passthrough over the LOG_SEARCH port (M4 wave-1).

POST /logs/search translates the request into the port's (LogQuery, TimeWindow,
Page) call. The window defaults to the last hour, computed server-side, and the
effective window is echoed back. `query.filters` become ANDed term filters —
by convention filters={"thread_id": <thread>} deep-links a pipeline run's logs
(runs tag their lines with the thread id in a later milestone; the dashboard's
GET ?thread=... deep link is built on this body shape).

Error mapping: provider-rejected queries (adapter ValueError, e.g. an ES 400
query_string parse failure with the ES reason extracted) -> 422 problem;
transport/upstream failures (RuntimeError, raw httpx errors) -> 502 problem.

Connection selection: optional `connection_id` query param (passthrough rule)
overrides the body field of the same name; otherwise the resolver picks the
project-scoped row (when the consumer is scoped to exactly one project), then
the global row, then the static stub.
"""

import math
from typing import Annotated, Any, cast

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from apex.app.dependencies import CurrentIdentity
from apex.domain.diagnostics import bounded_diagnostic
from apex.domain.durable_evidence import sanitize_durable_object
from apex.domain.input_limits import (
    MAX_JSON_DEPTH,
    MAX_JSON_KEY_CHARS,
    MAX_JSON_NODES,
    NoNulStr,
    RecordId,
    validate_json_object,
)
from apex.domain.integrations import LogEntry, LogQuery, LogSearchResult, Page
from apex.services.connections import close_adapter
from apex.services.log_search import (
    LogSearchResolver,
    effective_window,
    resolve_log_search_adapter,
)

router = APIRouter(prefix="/logs", tags=["logs"])
logger = structlog.get_logger(__name__)

MAX_LOG_FILTERS = 12
MAX_LOG_FILTER_VALUE_LENGTH = 512
MAX_LOG_TEXT_LENGTH = 2048
MAX_LOG_RESULT_WINDOW = 10_000
MAX_PUBLIC_LOG_FIELDS = 64
MAX_PUBLIC_LOG_FIELDS_BYTES = 64 * 1024
ALLOWED_LOG_FILTERS = frozenset(
    {
        "app_id",
        "container",
        "environment",
        "environment_id",
        "host",
        "kubernetes.labels.app",
        "kubernetes.namespace_name",
        "kubernetes.pod_name",
        "level",
        "namespace",
        "pod",
        "project_id",
        "service",
        "service.name",
        "span.id",
        "span_id",
        "thread_id",
        "trace.id",
        "trace_id",
    }
)


def get_log_search_resolver() -> LogSearchResolver:
    """Override point for tests; production resolves through connection rows."""
    return resolve_log_search_adapter


async def _close_log_adapter(adapter: Any) -> None:
    """Settle cleanup without replacing a stable error or successful response."""

    try:
        await close_adapter(adapter)
    except Exception:
        logger.warning("apex.logs.adapter_close_failed")


ResolverDep = Annotated[LogSearchResolver, Depends(get_log_search_resolver)]
ConnectionIdParam = Annotated[
    RecordId | None,
    Query(
        description="Explicit LOG_SEARCH connection to search through "
        "(overrides body.connection_id)."
    ),
]


# ── Schemas ──────────────────────────────────────────────────────────────────


class LogQueryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: NoNulStr | None = Field(
        default=None,
        max_length=MAX_LOG_TEXT_LENGTH,
        description="Free-text query (Lucene query_string syntax on ELK).",
    )
    filters: dict[str, str] = Field(
        default_factory=dict,
        description="ANDed exact-match filters (e.g. service, level); "
        "'thread_id' deep-links a pipeline run's logs by convention.",
    )

    @field_validator("filters")
    @classmethod
    def validate_filters(cls, filters: dict[str, str]) -> dict[str, str]:
        if len(filters) > MAX_LOG_FILTERS:
            raise ValueError(f"filters may contain at most {MAX_LOG_FILTERS} entries")
        validated: dict[str, str] = {}
        for raw_key, raw_value in filters.items():
            key = raw_key.strip()
            if key not in ALLOWED_LOG_FILTERS:
                raise ValueError(f"unsupported log filter field {raw_key!r}")
            value = raw_value.strip()
            if not value:
                raise ValueError(f"log filter {key!r} must not be blank")
            if "\x00" in value:
                raise ValueError(f"log filter {key!r} must not contain U+0000")
            if len(value) > MAX_LOG_FILTER_VALUE_LENGTH:
                raise ValueError(
                    f"log filter {key!r} must be at most {MAX_LOG_FILTER_VALUE_LENGTH} characters"
                )
            validated[key] = value
        return validated


class WindowIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    from_: NoNulStr | None = Field(
        default=None,
        alias="from",
        max_length=64,
        description="ISO-8601 lower bound.",
    )
    to: NoNulStr | None = Field(
        default=None,
        max_length=64,
        description="ISO-8601 upper bound.",
    )


class LogSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: LogQueryIn = Field(default_factory=LogQueryIn)
    window: WindowIn | None = Field(
        default=None, description="Defaults to the last hour, computed server-side."
    )
    connection_id: NoNulStr | None = Field(default=None, min_length=1, max_length=32)
    limit: int = Field(default=50, ge=1, le=500)
    offset: int = Field(default=0, ge=0, lt=MAX_LOG_RESULT_WINDOW)

    @model_validator(mode="after")
    def validate_result_window(self) -> "LogSearchRequest":
        if self.offset + self.limit > MAX_LOG_RESULT_WINDOW:
            raise ValueError(f"log result window must not exceed {MAX_LOG_RESULT_WINDOW} entries")
        return self


class WindowOut(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: str | None = Field(default=None, alias="from")
    to: str | None = None


class LogEntryOut(BaseModel):
    at: str
    level: str
    service: str
    message: str
    fields: dict[str, Any] = Field(
        default_factory=dict, description="Provider extras not consumed by the mapped columns."
    )


class LogSearchResponse(BaseModel):
    entries: list[LogEntryOut]
    total: int
    limit: int
    offset: int
    window: WindowOut


def _public_log_text(value: Any, *, max_chars: int, default: str) -> str:
    """Return one bounded, credential-redacted provider string."""

    if type(value) is not str or len(value) > max_chars or "\x00" in value:
        return default
    return bounded_diagnostic(value, max_chars=max(1, len(value)))


def _public_log_fields(value: Any) -> dict[str, Any]:
    """Project provider extras through fixed JSON and credential budgets."""

    if type(value) is not dict or len(value) > MAX_PUBLIC_LOG_FIELDS:
        raise ValueError("invalid log fields")
    # Clone only exact JSON built-ins before invoking shared validators. This
    # prevents provider-owned dict/list subclasses from lying about cardinality
    # or executing custom iteration while preserving ordinary ELK extras.
    cloned = _clone_exact_json(value)
    if type(cloned) is not dict:
        raise ValueError("invalid log fields")
    validate_json_object(
        cloned,
        label="log response fields",
        max_bytes=MAX_PUBLIC_LOG_FIELDS_BYTES,
    )
    return sanitize_durable_object(cloned)


def _clone_exact_json(value: Any) -> Any:
    """Copy one finite exact-built-in JSON value under the public log budget."""

    nodes = 0
    text_bytes = 0

    def clone(current: Any, *, depth: int) -> Any:
        nonlocal nodes, text_bytes
        nodes += 1
        if nodes > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            raise ValueError("log fields exceed the JSON structure budget")
        if current is None or type(current) is bool:
            return current
        if type(current) is int:
            if current.bit_length() > 256:
                raise ValueError("log fields contain an oversized integer")
            return current
        if type(current) is float:
            if not math.isfinite(current):
                raise ValueError("log fields contain a non-finite number")
            return current
        if type(current) is str:
            if "\x00" in current or len(current) > MAX_PUBLIC_LOG_FIELDS_BYTES:
                raise ValueError("log fields contain an invalid string")
            text_bytes += len(current.encode("utf-8"))
            if text_bytes > MAX_PUBLIC_LOG_FIELDS_BYTES:
                raise ValueError("log fields exceed the text budget")
            return current
        if type(current) is list:
            if len(current) > MAX_JSON_NODES - nodes:
                raise ValueError("log fields exceed the JSON node budget")
            return [clone(child, depth=depth + 1) for child in current]
        if type(current) is dict:
            if len(current) > MAX_JSON_NODES - nodes:
                raise ValueError("log fields exceed the JSON node budget")
            copied: dict[str, Any] = {}
            for key, child in current.items():
                if type(key) is not str or not 1 <= len(key) <= MAX_JSON_KEY_CHARS or "\x00" in key:
                    raise ValueError("log fields contain an invalid key")
                text_bytes += len(key.encode("utf-8"))
                if text_bytes > MAX_PUBLIC_LOG_FIELDS_BYTES:
                    raise ValueError("log fields exceed the text budget")
                copied[key] = clone(child, depth=depth + 1)
            return copied
        raise ValueError("log fields contain a non-JSON value")

    return clone(value, depth=0)


def _model_state(value: BaseModel) -> dict[str, Any]:
    """Read BaseModel storage without invoking subclass field descriptors."""

    state_descriptor = cast(Any, BaseModel.__dict__["__dict__"])
    extra_descriptor = cast(Any, BaseModel.__dict__["__pydantic_extra__"])
    state = state_descriptor.__get__(value, type(value))
    extras = extra_descriptor.__get__(value, type(value))
    if type(state) is not dict or extras is not None:
        raise ValueError("invalid provider model state")
    return cast(dict[str, Any], state)


def _log_entry_values(entry: Any) -> tuple[str, str, str, str, dict[str, Any]]:
    # Avoid ``isinstance`` here: after an exact-type miss it may read a hostile
    # provider object's spoofed ``__class__`` descriptor.
    if not issubclass(type(entry), LogEntry):
        raise ValueError("invalid log entry")
    raw = _model_state(entry)
    required = {"at", "level", "message", "service"}
    allowed = required | {"fields"}
    if len(raw) > len(allowed):
        raise ValueError("invalid log entry")
    if any(type(key) is not str for key in raw):
        raise ValueError("invalid log entry")
    keys = set(raw)
    if not required <= keys or not keys <= allowed:
        raise ValueError("invalid log entry")
    at = raw["at"]
    level = raw["level"]
    service = raw["service"]
    message = raw["message"]
    if (
        type(at) is not str
        or len(at) > 128
        or "\x00" in at
        or type(level) is not str
        or len(level) > 64
        or "\x00" in level
        or type(service) is not str
        or len(service) > 255
        or "\x00" in service
        or type(message) is not str
        or len(message) > 20_000
        or "\x00" in message
    ):
        raise ValueError("invalid log entry")
    fields = _public_log_fields(raw.get("fields", {}))
    return at, level, service, message, fields


def _project_public_log_result(
    result: Any, *, requested_limit: int
) -> tuple[list[LogEntryOut], int]:
    if type(result) is not LogSearchResult:
        raise ValueError("invalid log result")
    raw = _model_state(result)
    if any(type(key) is not str for key in raw):
        raise ValueError("invalid log result")
    if len(raw) != 2 or set(raw) != {"entries", "total"}:
        raise ValueError("invalid log result")
    entries = raw["entries"]
    total = raw["total"]
    if (
        type(entries) is not list
        or len(entries) > requested_limit
        or type(total) is not int
        or not 0 <= total <= 9_223_372_036_854_775_807
    ):
        raise ValueError("invalid log result")

    projected: list[LogEntryOut] = []
    for entry in entries:
        at, level, service, message, fields = _log_entry_values(entry)
        projected.append(
            LogEntryOut(
                at=_public_log_text(at, max_chars=128, default=""),
                level=_public_log_text(level, max_chars=64, default="INFO"),
                service=_public_log_text(service, max_chars=255, default=""),
                message=_public_log_text(message, max_chars=20_000, default=""),
                fields=fields,
            )
        )
    return projected, total


def _public_log_result(result: Any, *, requested_limit: int) -> tuple[list[LogEntryOut], int]:
    """Revalidate an adapter result before it crosses the HTTP boundary."""

    invalid = False
    try:
        return _project_public_log_result(result, requested_limit=requested_limit)
    except Exception:
        invalid = True
    # Provider models and fields can contain secret-bearing values. Drop the
    # provider object and raise after its handler so neither an exception chain
    # nor this stable error's traceback retains the hostile projection object.
    result = None
    if invalid:
        raise ValueError("invalid log result")
    raise AssertionError("log projection failure was not recorded")  # pragma: no cover


# ── Routes ───────────────────────────────────────────────────────────────────


@router.post("/search", operation_id="searchLogs", response_model=LogSearchResponse)
async def search_logs(
    body: LogSearchRequest,
    identity: CurrentIdentity,
    resolver: ResolverDep,
    connection_id: ConnectionIdParam = None,
) -> LogSearchResponse:
    """Search logs through the configured LOG_SEARCH connection (any authenticated role)."""
    window_error: HTTPException | None = None
    window = None
    try:
        window = effective_window(
            body.window.from_ if body.window is not None else None,
            body.window.to if body.window is not None else None,
        )
    except ValueError:
        window_error = HTTPException(status_code=422, detail="invalid log search window")
    if window_error is not None:
        raise window_error
    assert window is not None

    filters = dict(body.query.filters)
    project_id = _effective_project_filter(identity, filters)
    resolver_error: HTTPException | None = None
    adapter: Any = None
    try:
        adapter = await resolver(connection_id or body.connection_id, project_id)
    except KeyError:
        resolver_error = HTTPException(status_code=404, detail="log-search connection not found")
    except ValueError:
        resolver_error = HTTPException(status_code=422, detail="log-search connection is invalid")
    except Exception:
        resolver_error = HTTPException(status_code=503, detail="log-search connection unavailable")
    if resolver_error is not None:
        raise resolver_error
    assert adapter is not None

    query = LogQuery(query=body.query.text or "", filters=filters)
    page = Page(offset=body.offset, limit=body.limit)
    search_error: HTTPException | None = None
    result: Any = None
    entries: list[LogEntryOut] | None = None
    total: int | None = None
    try:
        try:
            result = await adapter.search(query, window=window, page=page)
        except ValueError:
            search_error = HTTPException(status_code=422, detail="log provider rejected the query")
        except (RuntimeError, httpx.HTTPError):
            search_error = HTTPException(status_code=502, detail="log search upstream failure")
        except Exception:
            search_error = HTTPException(status_code=502, detail="log search upstream failure")

        if search_error is None:
            try:
                entries, total = _public_log_result(result, requested_limit=body.limit)
            except ValueError:
                result = None
                search_error = HTTPException(status_code=502, detail="log search upstream failure")
    finally:
        await _close_log_adapter(adapter)
    if search_error is not None:
        raise search_error
    assert entries is not None and total is not None
    return LogSearchResponse(
        entries=entries,
        total=total,
        limit=body.limit,
        offset=body.offset,
        window=WindowOut.model_validate({"from": window.start, "to": window.end}),
    )


def _effective_project_filter(identity: Any, filters: dict[str, str]) -> str | None:
    """Inject/verify the exact project and app boundary for scoped consumers."""

    if identity.is_unscoped:
        return filters.get("project_id")
    allowed_projects = identity.scoped_project_ids()
    requested_project = filters.get("project_id")
    if requested_project is not None:
        if requested_project not in allowed_projects:
            raise HTTPException(
                status_code=403,
                detail="project is outside this consumer's scopes",
            )
        project_id = requested_project
    elif len(allowed_projects) == 1:
        project_id = allowed_projects[0]
        filters["project_id"] = project_id
    else:
        raise HTTPException(
            status_code=403,
            detail="project_id filter is required for consumers scoped to multiple projects",
        )

    requested_app = filters.get("app_id")
    if requested_app is not None:
        if not identity.allows_scope(project_id=project_id, app_id=requested_app):
            raise HTTPException(
                status_code=403,
                detail="app is outside this consumer's scopes",
            )
        return project_id

    project_wide = any(
        scope.project_id == project_id and scope.app_id is None for scope in identity.scopes
    )
    if project_wide:
        return project_id

    app_ids = tuple(
        dict.fromkeys(
            scope.app_id
            for scope in identity.scopes
            if scope.project_id == project_id and scope.app_id is not None
        )
    )
    if len(app_ids) == 1:
        filters["app_id"] = app_ids[0]
        return project_id
    raise HTTPException(
        status_code=403,
        detail="app_id filter is required for consumers scoped to multiple apps",
    )
