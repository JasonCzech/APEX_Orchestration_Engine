"""Agent-behavior analytics aggregation over apex.agent_events."""

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import ColumnElement, Select, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from apex.persistence.models import AgentEvent

AgentGroupBy = Literal["model", "stage", "agent", "date", "test"]
AgentSort = Literal[
    "key",
    "events",
    "errors",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
    "reasoning_tokens",
    "cost_usd",
    "avg_latency_ms",
    "p95_latency_ms",
    "runs",
]
AgentOrder = Literal["asc", "desc"]

TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
    "reasoning_tokens",
)


def _as_int(value: Any) -> int:
    return int(value or 0)


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


def _key_string(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if value is None or value == "":
        return "unknown"
    return str(value)


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class AgentAnalyticsRepository:
    """Postgres aggregation over agent_events with dashboard-shaped outputs."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def aggregate(
        self,
        *,
        window_from: datetime,
        window_to: datetime,
        bucket: str,
        group_by: AgentGroupBy,
        project_id: str | None = None,
        visible_project_ids: tuple[str, ...] | None = None,
        models: tuple[str, ...] = (),
        stages: tuple[str, ...] = (),
        agents: tuple[str, ...] = (),
        test: str | None = None,
        status: str | None = None,
        sort: AgentSort = "total_tokens",
        order: AgentOrder = "desc",
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        filters = self._filters(
            window_from=window_from,
            window_to=window_to,
            project_id=project_id,
            visible_project_ids=visible_project_ids,
            models=models,
            stages=stages,
            agents=agents,
            test=test,
            status=status,
        )
        metrics = self._metric_columns()
        totals_row = (await self._session.execute(select(*metrics).where(*filters))).one()
        totals = self._row_to_metrics(totals_row)
        totals["agents"] = _as_int(
            await self._session.scalar(
                select(func.count(func.distinct(AgentEvent.agent_name))).where(*filters)
            )
        )
        totals["models"] = _as_int(
            await self._session.scalar(
                select(func.count(func.distinct(AgentEvent.model))).where(*filters)
            )
        )

        group_col = self._group_column(group_by, bucket).label("key")
        count_stmt = select(func.count()).select_from(
            select(group_col).where(*filters).group_by(group_col).subquery()
        )
        total_rows = _as_int(await self._session.scalar(count_stmt))

        grouped_cols = [group_col, *metrics]
        grouped: Select[tuple[Any, ...]] = select(*grouped_cols).where(*filters).group_by(group_col)
        sort_expr = self._sort_expression(sort, group_col)
        grouped = grouped.order_by(
            sort_expr.asc() if order == "asc" else sort_expr.desc(),
            group_col.asc(),
        )
        grouped = grouped.limit(limit).offset(offset)
        breakdown_rows = (await self._session.execute(grouped)).all()
        breakdown = [
            {"key": _key_string(row[0]), "thread_id": row[0] if group_by == "test" else None}
            | self._row_to_metrics(row[1:])
            for row in breakdown_rows
        ]

        bucket_col = func.date_trunc(bucket, AgentEvent.at).label("bucket_start")
        series_rows = (
            await self._session.execute(
                select(bucket_col, group_col, *metrics)
                .where(*filters)
                .group_by(bucket_col, group_col)
                .order_by(bucket_col, group_col)
            )
        ).all()
        series = [
            {"bucket_start": row[0], "key": _key_string(row[1])} | self._row_to_metrics(row[2:])
            for row in series_rows
        ]

        return {
            "totals": totals,
            "breakdown": breakdown,
            "series": series,
            "page": {"limit": limit, "offset": offset, "total": total_rows},
        }

    def _filters(
        self,
        *,
        window_from: datetime,
        window_to: datetime,
        project_id: str | None,
        visible_project_ids: tuple[str, ...] | None,
        models: tuple[str, ...],
        stages: tuple[str, ...],
        agents: tuple[str, ...],
        test: str | None,
        status: str | None,
    ) -> list[ColumnElement[bool]]:
        filters: list[ColumnElement[bool]] = [
            AgentEvent.at >= window_from,
            AgentEvent.at < window_to,
        ]
        if project_id is not None:
            filters.append(AgentEvent.project_id == project_id)
        elif visible_project_ids is not None:
            filters.append(
                or_(
                    AgentEvent.project_id.is_(None),
                    AgentEvent.project_id.in_(visible_project_ids),
                )
            )
        if models:
            filters.append(AgentEvent.model.in_(models))
        if stages:
            filters.append(AgentEvent.phase.in_(stages))
        if agents:
            filters.append(AgentEvent.agent_name.in_(agents))
        if test:
            filters.append(AgentEvent.thread_id.ilike(f"%{_escape_like(test)}%", escape="\\"))
        if status:
            filters.append(AgentEvent.status == status)
        return filters

    def _metric_columns(self) -> list[Any]:
        error_count = func.sum(case((AgentEvent.status == "error", 1), else_=0)).label("errors")
        p95_latency = func.percentile_cont(0.95).within_group(AgentEvent.latency_ms).label(
            "p95_latency_ms"
        )
        return [
            func.count().label("events"),
            error_count,
            func.coalesce(func.sum(AgentEvent.input_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(AgentEvent.output_tokens), 0).label("output_tokens"),
            func.coalesce(func.sum(AgentEvent.total_tokens), 0).label("total_tokens"),
            func.coalesce(func.sum(AgentEvent.cache_read_tokens), 0).label("cache_read_tokens"),
            func.coalesce(func.sum(AgentEvent.cache_creation_tokens), 0).label(
                "cache_creation_tokens"
            ),
            func.coalesce(func.sum(AgentEvent.reasoning_tokens), 0).label("reasoning_tokens"),
            func.sum(AgentEvent.cost_usd).label("cost_usd"),
            func.avg(AgentEvent.latency_ms).label("avg_latency_ms"),
            p95_latency,
            func.count(func.distinct(AgentEvent.thread_id)).label("runs"),
        ]

    def _row_to_metrics(self, row: Any) -> dict[str, Any]:
        values = list(row)
        return {
            "events": _as_int(values[0]),
            "errors": _as_int(values[1]),
            "input_tokens": _as_int(values[2]),
            "output_tokens": _as_int(values[3]),
            "total_tokens": _as_int(values[4]),
            "cache_read_tokens": _as_int(values[5]),
            "cache_creation_tokens": _as_int(values[6]),
            "reasoning_tokens": _as_int(values[7]),
            "cost_usd": _as_float(values[8]),
            "avg_latency_ms": _as_float(values[9]),
            "p95_latency_ms": _as_float(values[10]),
            "runs": _as_int(values[11]),
        }

    def _group_column(self, group_by: AgentGroupBy, bucket: str) -> Any:
        if group_by == "model":
            return func.coalesce(AgentEvent.model, "unknown")
        if group_by == "stage":
            return AgentEvent.phase
        if group_by == "agent":
            return AgentEvent.agent_name
        if group_by == "test":
            return func.coalesce(AgentEvent.thread_id, "unknown")
        return func.date_trunc(bucket, AgentEvent.at)

    def _sort_expression(self, sort: AgentSort, group_col: Any) -> Any:
        if sort == "key":
            return group_col
        if sort == "events":
            return func.count()
        if sort == "errors":
            return func.sum(case((AgentEvent.status == "error", 1), else_=0))
        if sort == "cost_usd":
            return func.coalesce(func.sum(AgentEvent.cost_usd), 0)
        if sort == "avg_latency_ms":
            return func.coalesce(func.avg(AgentEvent.latency_ms), 0)
        if sort == "p95_latency_ms":
            return func.coalesce(func.percentile_cont(0.95).within_group(AgentEvent.latency_ms), 0)
        if sort == "runs":
            return func.count(func.distinct(AgentEvent.thread_id))
        column = getattr(AgentEvent, sort)
        return func.coalesce(func.sum(column), 0)
