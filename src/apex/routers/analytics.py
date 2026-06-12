"""/analytics: usage analytics over the best-effort usage_events projection (M6).

GET /analytics/usage (any authenticated role) aggregates the events emitted by the
/v1 usage middleware and the graph phase-finalize hook (apex.services.usage).
Scoping: unscoped admins see everything; scoped consumers see events in their
scoped projects plus project-less events (most /v1 requests carry no project).
An explicit ?project outside the consumer's scopes answers 403.
"""

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal, Protocol

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from apex.app.dependencies import CurrentIdentity
from apex.persistence.db import get_session
from apex.services.usage import UsageAnalyticsRepository

router = APIRouter(prefix="/analytics", tags=["analytics"])


class UsageAnalyticsReader(Protocol):
    """What the route needs from the aggregation layer (faked in unit tests)."""

    async def aggregate(
        self,
        *,
        window_from: datetime,
        window_to: datetime,
        bucket: str,
        project_id: str | None = None,
        visible_project_ids: tuple[str, ...] | None = None,
    ) -> dict[str, Any]: ...


def get_usage_analytics_repository(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> UsageAnalyticsReader:
    """Override point for tests; production aggregates on the request session."""
    return UsageAnalyticsRepository(session)


UsageRepo = Annotated[UsageAnalyticsReader, Depends(get_usage_analytics_repository)]

FromParam = Annotated[
    datetime | None,
    Query(alias="from", description="Window start (ISO-8601); default = `to` minus 7 days."),
]
ToParam = Annotated[
    datetime | None, Query(description="Window end (ISO-8601, exclusive); default = now.")
]
BucketParam = Annotated[Literal["day", "hour"], Query(description="Histogram bucket size.")]
ProjectParam = Annotated[
    str | None,
    Query(description="Filter to one project (must be inside the consumer's scopes)."),
]


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


# ── Routes ───────────────────────────────────────────────────────────────────


def _aware(value: datetime | None) -> datetime | None:
    """Naive query datetimes are taken as UTC."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


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
    window_from = _aware(from_) or window_to - timedelta(days=7)
    if window_from >= window_to:
        raise HTTPException(status_code=422, detail="`from` must be earlier than `to`")
    if project is not None and not identity.allows_project(project):
        raise HTTPException(
            status_code=403, detail=f"Project '{project}' is outside this consumer's scopes"
        )
    visible = None if identity.is_unscoped else identity.scoped_project_ids()
    data = await repo.aggregate(
        window_from=window_from,
        window_to=window_to,
        bucket=bucket,
        project_id=project,
        visible_project_ids=visible,
    )
    return UsageAnalyticsResponse(
        window=UsageWindow.model_validate({"from": window_from, "to": window_to, "bucket": bucket}),
        totals=UsageTotals(**data["totals"]),
        buckets=[UsageBucket(**row) for row in data["buckets"]],
        top_actions=[UsageTopAction(**row) for row in data["top_actions"]],
        runs=UsageRuns(**data["runs"]),
    )
