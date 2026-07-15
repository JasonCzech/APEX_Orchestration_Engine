"""/analytics: usage analytics over the best-effort usage_events projection (M6).

GET /analytics/usage (any authenticated role) aggregates the events emitted by the
/v1 usage middleware and the graph phase-finalize hook (apex.services.usage).
Scoping: unscoped admins see everything. Scoped consumers see only exact
project/application scopes; project-wide scopes include every application in
that project. Project-less rows are platform-only, and legacy rows without an
application id are hidden from app-only identities. An explicit ?project outside
the consumer's scopes answers 403.
"""

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal, Protocol

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from apex.app.dependencies import CurrentIdentity, SettingsDep
from apex.auth.identity import ConsumerIdentity, Role, ScopeRef
from apex.domain.input_limits import MAX_DB_LIST_OFFSET, NoNulStr, ScopeId
from apex.persistence.db import get_session
from apex.services.agent_analytics import (
    AgentAnalyticsRepository,
    AgentBucket,
    AgentGroupBy,
    AgentOrder,
    AgentSort,
)
from apex.services.usage import UsageAnalyticsRepository
from apex.settings import ApexSettings

router = APIRouter(prefix="/analytics", tags=["analytics"])

MAX_HOURLY_ANALYTICS_WINDOW = timedelta(days=31)
MAX_DAILY_ANALYTICS_WINDOW = timedelta(days=366)
MAX_ANALYTICS_FILTER_VALUES = 50
MAX_ANALYTICS_FILTER_CHARS = 255


class UsageAnalyticsReader(Protocol):
    """What the route needs from the aggregation layer (faked in unit tests)."""

    async def aggregate(
        self,
        *,
        window_from: datetime,
        window_to: datetime,
        bucket: str,
        project_id: str | None = None,
        visible_scopes: tuple[ScopeRef, ...] | None = None,
    ) -> dict[str, Any]: ...


class AgentAnalyticsReader(Protocol):
    """What the agent route needs from aggregation (faked in unit tests)."""

    async def aggregate(
        self,
        *,
        window_from: datetime,
        window_to: datetime,
        bucket: AgentBucket,
        group_by: AgentGroupBy,
        project_id: str | None = None,
        visible_scopes: tuple[ScopeRef, ...] | None = None,
        models: tuple[str, ...] = (),
        stages: tuple[str, ...] = (),
        agents: tuple[str, ...] = (),
        test: str | None = None,
        status: str | None = None,
        sort: AgentSort = "total_tokens",
        order: AgentOrder = "desc",
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]: ...


def get_usage_analytics_repository(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> UsageAnalyticsReader:
    """Override point for tests; production aggregates on the request session."""
    return UsageAnalyticsRepository(session)


def get_agent_analytics_repository(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AgentAnalyticsReader:
    return AgentAnalyticsRepository(session)


UsageRepo = Annotated[UsageAnalyticsReader, Depends(get_usage_analytics_repository)]
AgentRepo = Annotated[AgentAnalyticsReader, Depends(get_agent_analytics_repository)]

FromParam = Annotated[
    datetime | None,
    Query(alias="from", description="Window start (ISO-8601); default = `to` minus 7 days."),
]
ToParam = Annotated[
    datetime | None, Query(description="Window end (ISO-8601, exclusive); default = now.")
]
BucketParam = Annotated[Literal["day", "hour"], Query(description="Histogram bucket size.")]
ProjectParam = Annotated[
    ScopeId | None,
    Query(
        max_length=255,
        description="Filter to one project (must be inside the consumer's scopes).",
    ),
]
AnalyticsFilterValue = Annotated[
    NoNulStr,
    Field(max_length=MAX_ANALYTICS_FILTER_CHARS),
]
GroupByParam = Annotated[AgentGroupBy, Query(description="Breakdown dimension.")]
AgentSortParam = Annotated[AgentSort, Query(description="Breakdown sort metric.")]
AgentOrderParam = Annotated[AgentOrder, Query(description="Sort direction.")]
MultiParam = Annotated[
    list[AnalyticsFilterValue] | None,
    Query(
        max_length=MAX_ANALYTICS_FILTER_VALUES,
        description="Filter. Accepts repeated values or comma-separated values.",
    ),
]
TestParam = Annotated[
    NoNulStr | None,
    Query(max_length=64, description="Filter to one pipeline thread/test id."),
]
StatusParam = Annotated[Literal["ok", "error"] | None, Query(description="Agent event status.")]
LimitParam = Annotated[int, Query(ge=1, le=100)]
OffsetParam = Annotated[int, Query(ge=0, le=MAX_DB_LIST_OFFSET)]


# ── Schemas ──────────────────────────────────────────────────────────────────


class UsageWindow(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: datetime = Field(alias="from")
    to: datetime
    bucket: Literal["day", "hour"]


class UsageTotals(BaseModel):
    events: int
    errors: int
    by_surface: dict[str, int] = Field(
        default_factory=dict, description='Event counts keyed by surface ("v1", "graph").'
    )


class UsageBucket(BaseModel):
    bucket_start: datetime
    events: int
    errors: int


class UsageTopAction(BaseModel):
    action: str
    count: int


class UsageRuns(BaseModel):
    phases_succeeded: int
    phases_failed: int


class UsageAnalyticsResponse(BaseModel):
    window: UsageWindow
    totals: UsageTotals
    buckets: list[UsageBucket]
    top_actions: list[UsageTopAction] = Field(description="Top 10 actions by event count.")
    runs: UsageRuns


class AgentAnalyticsTotals(BaseModel):
    events: int
    errors: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    reasoning_tokens: int
    cost_usd: float | None = None
    avg_latency_ms: float | None = None
    p95_latency_ms: float | None = None
    runs: int
    agents: int = 0
    models: int = 0


class AgentAnalyticsBreakdownRow(BaseModel):
    key: str
    thread_id: str | None = None
    events: int
    errors: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    reasoning_tokens: int
    cost_usd: float | None = None
    avg_latency_ms: float | None = None
    p95_latency_ms: float | None = None
    runs: int


class AgentAnalyticsSeriesPoint(BaseModel):
    bucket_start: datetime
    key: str
    events: int
    errors: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    reasoning_tokens: int
    cost_usd: float | None = None
    avg_latency_ms: float | None = None
    p95_latency_ms: float | None = None
    runs: int


class AgentAnalyticsPage(BaseModel):
    limit: int
    offset: int
    total: int


class AgentAnalyticsWindow(UsageWindow):
    group_by: AgentGroupBy


class AgentAnalyticsResponse(BaseModel):
    window: AgentAnalyticsWindow
    totals: AgentAnalyticsTotals
    breakdown: list[AgentAnalyticsBreakdownRow]
    series: list[AgentAnalyticsSeriesPoint]
    page: AgentAnalyticsPage
    cost_visible: bool


# ── Routes ───────────────────────────────────────────────────────────────────


def _aware(value: datetime | None) -> datetime | None:
    """Naive query datetimes are taken as UTC."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _default_window_start(window_to: datetime) -> datetime:
    """Return the seven-day default without leaking datetime underflow as a 500."""

    try:
        return window_to - timedelta(days=7)
    except OverflowError as exc:
        raise HTTPException(
            status_code=422,
            detail="`to` is too early to derive the default seven-day analytics window",
        ) from exc


def _multi(values: list[str] | None) -> tuple[str, ...]:
    flattened: list[str] = []
    for value in values or []:
        flattened.extend(part.strip() for part in value.split(","))
        if len(flattened) > MAX_ANALYTICS_FILTER_VALUES:
            raise HTTPException(
                status_code=422,
                detail=f"analytics filters accept at most {MAX_ANALYTICS_FILTER_VALUES} values",
            )
    result = tuple(part for part in flattened if part)
    if len(result) > MAX_ANALYTICS_FILTER_VALUES:
        raise HTTPException(
            status_code=422,
            detail=f"analytics filters accept at most {MAX_ANALYTICS_FILTER_VALUES} values",
        )
    if any(len(part) > MAX_ANALYTICS_FILTER_CHARS for part in result):
        raise HTTPException(
            status_code=422,
            detail=(
                f"analytics filter values must not exceed {MAX_ANALYTICS_FILTER_CHARS} characters"
            ),
        )
    return result


def _validate_window(window_from: datetime, window_to: datetime, bucket: str) -> None:
    if window_from >= window_to:
        raise HTTPException(status_code=422, detail="`from` must be earlier than `to`")
    maximum = MAX_HOURLY_ANALYTICS_WINDOW if bucket == "hour" else MAX_DAILY_ANALYTICS_WINDOW
    if window_to - window_from > maximum:
        raise HTTPException(
            status_code=422,
            detail=f"{bucket} analytics windows must not exceed {maximum.days} days",
        )


def _cost_visible(identity: ConsumerIdentity, settings: ApexSettings) -> bool:
    return bool(settings.analytics_cost_visible and identity.role is Role.ADMIN)


def _scrub_costs(payload: dict[str, Any]) -> dict[str, Any]:
    payload["totals"]["cost_usd"] = None
    for row in payload["breakdown"]:
        row["cost_usd"] = None
    for row in payload["series"]:
        row["cost_usd"] = None
    return payload


@router.get("/usage", operation_id="getUsageAnalytics", response_model=UsageAnalyticsResponse)
async def get_usage_analytics(
    identity: CurrentIdentity,
    repo: UsageRepo,
    from_: FromParam = None,
    to: ToParam = None,
    bucket: BucketParam = "day",
    project: ProjectParam = None,
) -> UsageAnalyticsResponse:
    """Aggregate usage events (any authenticated role; results are scope-filtered)."""
    window_to = _aware(to) or datetime.now(UTC)
    window_from = _aware(from_) or _default_window_start(window_to)
    _validate_window(window_from, window_to, bucket)
    if project is not None and not identity.allows_project(project):
        raise HTTPException(status_code=403, detail="project is outside this consumer's scopes")
    visible = None if identity.is_unscoped else tuple(identity.scopes)
    data = await repo.aggregate(
        window_from=window_from,
        window_to=window_to,
        bucket=bucket,
        project_id=project,
        visible_scopes=visible,
    )
    return UsageAnalyticsResponse(
        window=UsageWindow.model_validate({"from": window_from, "to": window_to, "bucket": bucket}),
        totals=UsageTotals(**data["totals"]),
        buckets=[UsageBucket(**row) for row in data["buckets"]],
        top_actions=[UsageTopAction(**row) for row in data["top_actions"]],
        runs=UsageRuns(**data["runs"]),
    )


@router.get("/agents", operation_id="getAgentAnalytics", response_model=AgentAnalyticsResponse)
async def get_agent_analytics(
    identity: CurrentIdentity,
    repo: AgentRepo,
    settings: SettingsDep,
    from_: FromParam = None,
    to: ToParam = None,
    bucket: BucketParam = "day",
    group_by: GroupByParam = "model",
    project: ProjectParam = None,
    model: MultiParam = None,
    stage: MultiParam = None,
    agent: MultiParam = None,
    test: TestParam = None,
    status: StatusParam = None,
    sort: AgentSortParam = "total_tokens",
    order: AgentOrderParam = "desc",
    limit: LimitParam = 20,
    offset: OffsetParam = 0,
) -> AgentAnalyticsResponse:
    """Aggregate LangGraph agent behavior events."""
    window_to = _aware(to) or datetime.now(UTC)
    window_from = _aware(from_) or _default_window_start(window_to)
    _validate_window(window_from, window_to, bucket)
    if project is not None and not identity.allows_project(project):
        raise HTTPException(status_code=403, detail="project is outside this consumer's scopes")
    visible = None if identity.is_unscoped else tuple(identity.scopes)
    show_cost = _cost_visible(identity, settings)
    effective_sort = "total_tokens" if not show_cost and sort == "cost_usd" else sort
    models = _multi(model)
    stages = _multi(stage)
    agents = _multi(agent)
    data = await repo.aggregate(
        window_from=window_from,
        window_to=window_to,
        bucket=bucket,
        group_by=group_by,
        project_id=project,
        visible_scopes=visible,
        models=models,
        stages=stages,
        agents=agents,
        test=test,
        status=status,
        sort=effective_sort,
        order=order,
        limit=limit,
        offset=offset,
    )
    if not show_cost:
        data = _scrub_costs(data)
    return AgentAnalyticsResponse(
        window=AgentAnalyticsWindow.model_validate(
            {"from": window_from, "to": window_to, "bucket": bucket, "group_by": group_by}
        ),
        totals=AgentAnalyticsTotals(**data["totals"]),
        breakdown=[AgentAnalyticsBreakdownRow(**row) for row in data["breakdown"]],
        series=[AgentAnalyticsSeriesPoint(**row) for row in data["series"]],
        page=AgentAnalyticsPage(**data["page"]),
        cost_visible=show_cost,
    )
