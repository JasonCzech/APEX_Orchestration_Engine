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

from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from apex.app.dependencies import CurrentIdentity
from apex.domain.diagnostics import bounded_diagnostic
from apex.domain.durable_evidence import sanitize_durable_object
from apex.domain.input_limits import NoNulStr, RecordId, validate_json_object
from apex.domain.integrations import LogEntry, LogQuery, LogSearchResult, Page
from apex.services.log_search import (
    LogSearchResolver,
    effective_window,
    resolve_log_search_adapter,
)

router = APIRouter(prefix="/logs", tags=["logs"])

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

    if not isinstance(value, str) or len(value) > max_chars or "\x00" in value:
        return default
    return bounded_diagnostic(value, max_chars=max(1, len(value)))


def _public_log_fields(value: Any) -> dict[str, Any]:
    """Project provider extras through fixed JSON and credential budgets."""

    if not isinstance(value, dict) or len(value) > MAX_PUBLIC_LOG_FIELDS:
        return {}
    try:
        validate_json_object(
            value,
            label="log response fields",
            max_bytes=MAX_PUBLIC_LOG_FIELDS_BYTES,
        )
    except (RecursionError, TypeError, ValueError):
        return {}
    return sanitize_durable_object(value)


def _public_log_result(result: Any, *, requested_limit: int) -> tuple[list[LogEntryOut], int]:
    """Revalidate an adapter result before it crosses the HTTP boundary."""

    if not isinstance(result, LogSearchResult):
        raise ValueError("invalid log result")
    entries = result.entries
    total = result.total
    if (
        not isinstance(entries, list)
        or len(entries) > requested_limit
        or isinstance(total, bool)
        or not isinstance(total, int)
        or not 0 <= total <= 9_223_372_036_854_775_807
    ):
        raise ValueError("invalid log result")

    projected: list[LogEntryOut] = []
    for entry in entries:
        normalized = LogEntry.model_validate(
            {
                "at": getattr(entry, "at", None),
                "level": getattr(entry, "level", None),
                "service": getattr(entry, "service", None),
                "message": getattr(entry, "message", None),
            }
        )
        projected.append(
            LogEntryOut(
                at=_public_log_text(normalized.at, max_chars=128, default=""),
                level=_public_log_text(normalized.level, max_chars=64, default="INFO"),
                service=_public_log_text(normalized.service, max_chars=255, default=""),
                message=_public_log_text(normalized.message, max_chars=20_000, default=""),
                fields=_public_log_fields(getattr(entry, "fields", None)),
            )
        )
    return projected, total


# ── Routes ───────────────────────────────────────────────────────────────────


@router.post("/search", operation_id="searchLogs", response_model=LogSearchResponse)
async def search_logs(
    body: LogSearchRequest,
    identity: CurrentIdentity,
    resolver: ResolverDep,
    connection_id: ConnectionIdParam = None,
) -> LogSearchResponse:
    """Search logs through the configured LOG_SEARCH connection (any authenticated role)."""
    try:
        window = effective_window(
            body.window.from_ if body.window is not None else None,
            body.window.to if body.window is not None else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid log search window") from exc

    filters = dict(body.query.filters)
    project_id = _effective_project_filter(identity, filters)
    try:
        adapter = await resolver(connection_id or body.connection_id, project_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="log-search connection not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="log-search connection is invalid") from exc

    query = LogQuery(query=body.query.text or "", filters=filters)
    page = Page(offset=body.offset, limit=body.limit)
    try:
        result = await adapter.search(query, window=window, page=page)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="log provider rejected the query") from exc
    except (RuntimeError, httpx.HTTPError) as exc:
        raise HTTPException(status_code=502, detail="log search upstream failure") from exc

    try:
        entries, total = _public_log_result(result, requested_limit=body.limit)
    except (AttributeError, TypeError, ValueError, ValidationError) as exc:
        raise HTTPException(status_code=502, detail="log search upstream failure") from exc
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
                detail=(
                    f"App '{requested_app}' in project '{project_id}' is outside "
                    "this consumer's scopes"
                ),
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
