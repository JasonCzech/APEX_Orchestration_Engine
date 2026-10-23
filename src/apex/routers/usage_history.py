"""Private event history and bounded, scope-identical CSV export."""

import csv
import io
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from apex.app.dependencies import CurrentIdentity, SettingsDep
from apex.domain.diagnostics import bounded_diagnostic
from apex.domain.input_limits import NoNulStr, RecordId, ScopeId
from apex.persistence.db import get_session
from apex.persistence.models import (
    AgentEvent,
    ModelCatalogEntry,
    ModelPriceInterval,
    ModelPricingGeneration,
    UsageCostAdjustment,
)
from apex.services.agent_analytics import event_provider
from apex.services.analytics_scope import AnalyticsVisibility
from apex.services.model_pricing import effective_cost, utc

router = APIRouter(prefix="/analytics/agents", tags=["analytics"])


async def history_session(session: Annotated[AsyncSession, Depends(get_session)]) -> AsyncSession:
    if not session.in_transaction() and session.get_bind().dialect.name == "postgresql":
        await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
    return session


Session = Annotated[AsyncSession, Depends(history_session)]


class AgentUsageEventResponse(BaseModel):
    id: str
    at: datetime
    model: str | None
    provider: str | None
    model_catalog_id: str | None
    project_id: str | None
    app_id: str | None
    consumer_id: str | None
    consumer_name_snapshot: str | None
    thread_id: str | None
    phase: str
    agent_name: str
    status: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    reasoning_tokens: int
    latency_ms: int | None
    archived: bool
    original_cost_usd: str | None
    effective_cost_usd: str | None
    pricing_interval_id: str | None


class AgentUsageHistoryResponse(BaseModel):
    items: list[AgentUsageEventResponse]
    next_cursor: str | None
    cost_visible: bool


class AgentUsageDetailResponse(AgentUsageEventResponse):
    cost_visible: bool
    original_pricing: dict[str, Any] | None = None
    corrections: list[dict[str, Any]] = []


class HistoryFilter:
    def __init__(
        self,
        from_: Annotated[datetime | None, Query(alias="from")] = None,
        to: datetime | None = None,
        view: Literal["mine", "authorized"] | None = None,
        consumer_id: Annotated[NoNulStr | None, Query(max_length=255)] = None,
        project: Annotated[ScopeId | None, Query(max_length=255)] = None,
        app: Annotated[ScopeId | None, Query(max_length=255)] = None,
        model_id: Annotated[RecordId | None, Query(max_length=32)] = None,
        provider: Annotated[NoNulStr | None, Query(max_length=64)] = None,
        model: Annotated[NoNulStr | None, Query(max_length=12799)] = None,
        stage: Annotated[NoNulStr | None, Query(max_length=12799)] = None,
        agent: Annotated[NoNulStr | None, Query(max_length=12799)] = None,
        test: Annotated[NoNulStr | None, Query(max_length=64)] = None,
        status: Literal["ok", "error"] | None = None,
        cost_basis: Literal["original", "corrected"] = "corrected",
    ):
        from apex.routers.analytics import _default_window_start

        self.end = to or datetime.now(UTC)
        self.start = from_ or _default_window_start(self.end)
        if self.start.tzinfo is None or self.end.tzinfo is None or self.start >= self.end:
            raise HTTPException(422, "Use a non-empty timezone-aware date interval")
        self.view: Literal["mine", "authorized"] | None = view
        self.consumer_id, self.project, self.app = consumer_id, project, app
        self.model_id, self.model, self.cost_basis = model_id, model, cost_basis
        self.provider = provider
        self.stage, self.agent, self.test, self.status = stage, agent, test, status

    def predicates(self, identity: Any) -> list[Any]:
        if self.project is not None and not identity.allows_project(self.project):
            raise HTTPException(403, "Project outside authorized scope")
        if self.app is not None and (
            self.project is None
            or not identity.allows_scope(project_id=self.project, app_id=self.app)
        ):
            raise HTTPException(403, "Application outside authorized scope")
        filters = AnalyticsVisibility.resolve(identity, self.view, self.consumer_id).filters(
            AgentEvent
        )
        filters += [AgentEvent.at >= self.start, AgentEvent.at < self.end]
        if self.provider is not None:
            filters.append(event_provider() == self.provider)
        for attr, value in [
            ("project_id", self.project),
            ("app_id", self.app),
            ("model_catalog_id", self.model_id),
            ("status", self.status),
        ]:
            if value is not None:
                filters.append(getattr(AgentEvent, attr) == value)
        from apex.routers.analytics import _multi

        for column, value in [
            (AgentEvent.model, self.model),
            (AgentEvent.phase, self.stage),
            (AgentEvent.agent_name, self.agent),
        ]:
            if value:
                filters.append(column.in_(_multi([value])))
        if self.test:
            from apex.services.agent_analytics import _escape_like

            filters.append(AgentEvent.thread_id.ilike(f"%{_escape_like(self.test)}%", escape="\\"))
        return filters


Filters = Annotated[HistoryFilter, Depends()]


def event_query(filters: list[Any]) -> Any:
    return (
        select(AgentEvent, effective_cost(), ModelCatalogEntry.archived_at)
        .outerjoin(ModelCatalogEntry, ModelCatalogEntry.id == AgentEvent.model_catalog_id)
        .where(*filters)
        .order_by(AgentEvent.at.desc(), AgentEvent.id.desc())
    )


def render(
    event: AgentEvent, cost: Decimal | None, archived: Any, show_cost: bool
) -> dict[str, Any]:
    result = {
        key: getattr(event, key)
        for key in (
            "id",
            "at",
            "model",
            "provider",
            "model_catalog_id",
            "project_id",
            "app_id",
            "consumer_id",
            "consumer_name_snapshot",
            "thread_id",
            "phase",
            "agent_name",
            "status",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cache_read_tokens",
            "cache_creation_tokens",
            "reasoning_tokens",
            "latency_ms",
        )
    }
    # Older rows and direct repairs may predate durable-text validation. Match
    # aggregate projections at the detail/export boundary, including user labels.
    for key, limit in {
        "id": 32,
        "model": 255,
        "provider": 64,
        "model_catalog_id": 32,
        "project_id": 255,
        "app_id": 255,
        "consumer_id": 255,
        "consumer_name_snapshot": 255,
        "thread_id": 64,
        "phase": 64,
        "agent_name": 255,
        "status": 16,
    }.items():
        if result[key] is not None:
            result[key] = bounded_diagnostic(result[key], max_chars=limit)
    result.update(
        archived=archived is not None,
        original_cost_usd=str(event.cost_usd) if show_cost and event.cost_usd is not None else None,
        effective_cost_usd=str(cost) if show_cost and cost is not None else None,
        pricing_interval_id=event.pricing_interval_id if show_cost else None,
    )
    return result


@router.get(
    "/events", operation_id="getAgentUsageHistory", response_model=AgentUsageHistoryResponse
)
async def history(
    identity: CurrentIdentity,
    session: Session,
    settings: SettingsDep,
    filters: Filters,
    cursor: Annotated[NoNulStr | None, Query(max_length=100)] = None,
    limit: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    predicates = filters.predicates(identity)
    if cursor:
        try:
            raw_at, event_id = cursor.rsplit("|", 1)
            at = datetime.fromisoformat(raw_at)
            if at.tzinfo is None:
                raise ValueError
        except ValueError:
            raise HTTPException(422, "Invalid history cursor") from None
        predicates.append(
            (AgentEvent.at < at) | ((AgentEvent.at == at) & (AgentEvent.id < event_id))
        )
    rows = (await session.execute(event_query(predicates).limit(limit + 1))).all()
    next_cursor = None
    if len(rows) > limit:
        last = rows[limit - 1][0]
        next_cursor = f"{utc(last.at).isoformat()}|{last.id}"
    return {
        "items": [
            render(
                event,
                event.cost_usd if filters.cost_basis == "original" else cost,
                archived,
                settings.analytics_cost_visible,
            )
            for event, cost, archived in rows[:limit]
        ],
        "next_cursor": next_cursor,
        "cost_visible": settings.analytics_cost_visible,
    }


@router.get(
    "/events/{event_id}", operation_id="getAgentUsageEvent", response_model=AgentUsageDetailResponse
)
async def detail(
    event_id: RecordId,
    identity: CurrentIdentity,
    session: Session,
    settings: SettingsDep,
    view: Literal["mine", "authorized"] | None = None,
) -> dict[str, Any]:
    filters = AnalyticsVisibility.resolve(identity, view).filters(AgentEvent) + [
        AgentEvent.id == event_id
    ]
    row = (await session.execute(event_query(filters))).first()
    if row is None:
        raise HTTPException(404, "Usage event not found")
    event, cost, archived = row
    result = render(event, cost, archived, settings.analytics_cost_visible)
    result["cost_visible"] = settings.analytics_cost_visible
    if settings.analytics_cost_visible:
        original = (
            await session.get(ModelPriceInterval, event.pricing_interval_id)
            if event.pricing_interval_id
            else None
        )
        from apex.routers.model_pricing import Rates, serial

        result["original_pricing"] = serial(original) if original else None
        if original is None:
            snapshot = event.extra.get("pricing") if type(event.extra) is dict else None
            if type(snapshot) is dict:
                try:
                    rates = Rates.model_validate(snapshot)
                except ValueError:
                    pass
                else:
                    result["original_pricing"] = {
                        key: format(rate, "f") for key, rate in rates.model_dump().items()
                    }
        changes = (
            await session.execute(
                select(UsageCostAdjustment, ModelPricingGeneration, ModelPriceInterval)
                .join(
                    ModelPricingGeneration,
                    ModelPricingGeneration.id == UsageCostAdjustment.generation_id,
                )
                .join(
                    ModelPriceInterval,
                    ModelPriceInterval.id == UsageCostAdjustment.interval_id,
                )
                .where(
                    UsageCostAdjustment.event_id == event.id,
                    ModelPricingGeneration.published_at.is_not(None),
                )
                .order_by(ModelPricingGeneration.number)
            )
        ).all()
        result["corrections"] = [
            {
                "cost_usd": str(a.cost_usd),
                "interval_id": a.interval_id,
                "at": g.published_at,
                "generation": g.number,
                "pricing": serial(interval),
            }
            for a, g, interval in changes
        ]
    return result


def csv_cell(value: Any) -> Any:
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r", "\n")):
        return "'" + value
    return value


@router.get("/export", operation_id="exportAgentUsage")
async def export(
    identity: CurrentIdentity, session: Session, settings: SettingsDep, filters: Filters
) -> StreamingResponse:
    predicates = filters.predicates(identity)
    count = await session.scalar(select(func.count()).select_from(AgentEvent).where(*predicates))
    if count and count > 100_000:
        raise HTTPException(422, "Export exceeds 100,000 events; narrow the interval")
    # Materialize bounded IDs in this transaction to keep the cap stable while new events arrive.
    ids = list(
        (
            await session.scalars(
                select(AgentEvent.id)
                .where(*predicates)
                .order_by(AgentEvent.at.desc(), AgentEvent.id.desc())
                .limit(100_001)
            )
        ).all()
    )
    if len(ids) > 100_000:
        raise HTTPException(422, "Export exceeds 100,000 events; narrow the interval")

    async def stream() -> AsyncIterator[str]:
        columns = [
            "id",
            "at",
            "model",
            "provider",
            "project_id",
            "app_id",
            "consumer_id",
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_creation_tokens",
            "total_tokens",
            "original_cost_usd",
            "effective_cost_usd",
            "pricing_interval_id",
        ]
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(columns)
        yield buffer.getvalue()
        for start in range(0, len(ids), 500):
            rows = (
                await session.execute(
                    event_query(predicates + [AgentEvent.id.in_(ids[start : start + 500])])
                )
            ).all()
            buffer.seek(0)
            buffer.truncate(0)
            for event, cost, archived in rows:
                item = render(
                    event,
                    event.cost_usd if filters.cost_basis == "original" else cost,
                    archived,
                    settings.analytics_cost_visible,
                )
                writer.writerow([csv_cell(item.get(c)) for c in columns])
            yield buffer.getvalue()

    return StreamingResponse(
        stream(),
        media_type="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="agent-usage.csv"',
            "Cache-Control": "no-store",
        },
    )


class UsageFacetOption(BaseModel):
    value: str
    label: str
    project_id: str | None = None
    app_id: str | None = None


class UsageFacetsResponse(BaseModel):
    applications: list[UsageFacetOption]
    users: list[UsageFacetOption]
    truncated: bool


@router.get("/facets", operation_id="getAgentUsageFacets", response_model=UsageFacetsResponse)
async def facets(
    identity: CurrentIdentity,
    session: Session,
    filters: Filters,
    q: Annotated[NoNulStr, Query(max_length=255)] = "",
) -> dict[str, Any]:
    from apex.auth.identity import Role
    from apex.persistence.models import Application, ProjectApplication

    predicates = filters.predicates(identity)
    # Name the application only where it is (still) linked to the event's project.
    linked = (
        select(ProjectApplication.application_id)
        .where(
            ProjectApplication.application_id == AgentEvent.app_id,
            ProjectApplication.project_id == AgentEvent.project_id,
        )
        .exists()
    )
    app_query = (
        select(AgentEvent.project_id, AgentEvent.app_id, func.max(Application.name))
        .outerjoin(Application, (Application.id == AgentEvent.app_id) & linked)
        .where(*predicates, AgentEvent.project_id.is_not(None), AgentEvent.app_id.is_not(None))
        .group_by(AgentEvent.project_id, AgentEvent.app_id)
        .order_by(AgentEvent.project_id, AgentEvent.app_id)
        .limit(201)
    )
    if q:
        app_query = app_query.where(
            AgentEvent.app_id.contains(q, autoescape=True)
            | Application.name.contains(q, autoescape=True)
        )
    applications = (await session.execute(app_query)).all()
    users = []
    if identity.role is Role.ADMIN and filters.view != "mine":
        user_query = (
            select(AgentEvent.consumer_id, func.max(AgentEvent.consumer_name_snapshot))
            .where(*predicates, AgentEvent.consumer_id.is_not(None))
            .group_by(AgentEvent.consumer_id)
            .order_by(AgentEvent.consumer_id)
            .limit(201)
        )
        if q:
            user_query = user_query.where(
                AgentEvent.consumer_id.contains(q, autoescape=True)
                | AgentEvent.consumer_name_snapshot.contains(q, autoescape=True)
            )
        users = list((await session.execute(user_query)).all())
    return {
        "applications": [
            {
                "value": f"{len(project)}:{project}{len(app)}:{app}",
                "label": bounded_diagnostic(name or app, max_chars=255),
                "project_id": project,
                "app_id": app,
            }
            for project, app, name in applications[:200]
        ],
        "users": [
            {"value": user, "label": bounded_diagnostic(name or user, max_chars=255)}
            for user, name in users[:200]
        ],
        "truncated": len(applications) > 200 or len(users) > 200,
    }
