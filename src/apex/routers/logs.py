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
from pydantic import BaseModel, ConfigDict, Field, field_validator

from apex.app.dependencies import CurrentIdentity
from apex.domain.integrations import LogQuery, Page
from apex.services.log_search import (
    LogSearchResolver,
    effective_window,
    resolve_log_search_adapter,
)

router = APIRouter(prefix="/logs", tags=["logs"])

MAX_LOG_FILTERS = 12
MAX_LOG_FILTER_VALUE_LENGTH = 512
MAX_LOG_TEXT_LENGTH = 2048
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
    str | None,
    Query(
        description="Explicit LOG_SEARCH connection to search through "
        "(overrides body.connection_id)."
    ),
]


# ── Schemas ──────────────────────────────────────────────────────────────────


class LogQueryIn(BaseModel):
    text: str | None = Field(
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
            if len(value) > MAX_LOG_FILTER_VALUE_LENGTH:
                raise ValueError(
                    f"log filter {key!r} must be at most {MAX_LOG_FILTER_VALUE_LENGTH} characters"
                )
            validated[key] = value
        return validated


class WindowIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: str | None = Field(default=None, alias="from", description="ISO-8601 lower bound.")
    to: str | None = Field(default=None, description="ISO-8601 upper bound.")


class LogSearchRequest(BaseModel):
    query: LogQueryIn = Field(default_factory=LogQueryIn)
    window: WindowIn | None = Field(
        default=None, description="Defaults to the last hour, computed server-side."
    )
    connection_id: str | None = None
    limit: int = Field(default=50, ge=1, le=500)
    offset: int = Field(default=0, ge=0)


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
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    scoped = identity.scoped_project_ids()
    project_id = scoped[0] if len(scoped) == 1 else None
    try:
        adapter = await resolver(connection_id or body.connection_id, project_id)
    except KeyError as exc:
        detail = str(exc.args[0]) if exc.args else "log-search connection not found"
        raise HTTPException(status_code=404, detail=detail) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    query = LogQuery(query=body.query.text or "", filters=dict(body.query.filters))
    page = Page(offset=body.offset, limit=body.limit)
    try:
        result = await adapter.search(query, window=window, page=page)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="log provider rejected the query") from exc
    except (RuntimeError, httpx.HTTPError) as exc:
        raise HTTPException(status_code=502, detail="log search upstream failure") from exc

    entries = [
        LogEntryOut(
            at=entry.at,
            level=entry.level,
            service=entry.service,
            message=entry.message,
            fields=dict(getattr(entry, "fields", None) or {}),
        )
        for entry in result.entries
    ]
    return LogSearchResponse(
        entries=entries,
        total=result.total,
        limit=body.limit,
        offset=body.offset,
        window=WindowOut.model_validate({"from": window.start, "to": window.end}),
    )
