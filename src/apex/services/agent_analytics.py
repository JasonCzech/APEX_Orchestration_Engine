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
AgentBucket = Literal["day", "hour"]

# Cap on distinct series keys for non-date group-bys so a high-cardinality
# dimension (e.g. test) can't emit a series row per key per bucket (review R5).
SERIES_KEY_CAP = 12

# Measure order shared by the SELECT list and _row_to_metrics (kept in lockstep).
_METRIC_ORDER = (
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
)
_FLOAT_METRICS = frozenset({"cost_usd", "avg_latency_ms", "p95_latency_ms"})

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
        bucket: AgentBucket,
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

        group_expr = self._group_column(group_by, bucket)
        group_col = group_expr.label("key")
        count_stmt = select(func.count()).select_from(
            select(group_col).where(*filters).group_by(group_col).subquery()
        )
        total_rows = _as_int(await self._session.scalar(count_stmt))

        sort_expr = self._sort_expression(sort, group_col)
        ordered = (sort_expr.asc() if order == "asc" else sort_expr.desc(), group_col.asc())
        grouped_cols = [group_col, *metrics]
        grouped: Select[tuple[Any, ...]] = (
            select(*grouped_cols)
            .where(*filters)
            .group_by(group_col)
            .order_by(*ordered)
            .limit(limit)
            .offset(offset)
        )
        breakdown_rows = (await self._session.execute(grouped)).all()
        breakdown = [
            {"key": _key_string(row[0]), "thread_id": row[0] if group_by == "test" else None}
            | self._row_to_metrics(row[1:])
            for row in breakdown_rows
        ]

        bucket_col = func.date_trunc(bucket, AgentEvent.at).label("bucket_start")
        series_filters = filters
        if group_by != "date":
            # Restrict the time-series to the top-N keys by the active sort so a
            # high-cardinality group_by can't return a row per key per bucket (review R5).
            # `date` grouping is already bounded by the number of window buckets.
            key_rows = (
                await self._session.execute(
                    select(group_col)
                    .where(*filters)
                    .group_by(group_col)
                    .order_by(*ordered)
                    .limit(SERIES_KEY_CAP)
                )
            ).all()
            series_filters = [*filters, group_expr.in_([row[0] for row in key_rows])]
        series_rows = (
            await self._session.execute(
                select(bucket_col, group_col, *metrics)
                .where(*series_filters)
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

    def _metric_map(self) -> dict[str, Any]:
        """Single source of truth for every measure expression (used by the SELECT
        list and by _sort_expression, so the sort key can never drift from the
        displayed value — review R12)."""
        return {
            "events": func.count(),
            "errors": func.sum(case((AgentEvent.status == "error", 1), else_=0)),
            "input_tokens": func.coalesce(func.sum(AgentEvent.input_tokens), 0),
            "output_tokens": func.coalesce(func.sum(AgentEvent.output_tokens), 0),
            "total_tokens": func.coalesce(func.sum(AgentEvent.total_tokens), 0),
            "cache_read_tokens": func.coalesce(func.sum(AgentEvent.cache_read_tokens), 0),
            "cache_creation_tokens": func.coalesce(func.sum(AgentEvent.cache_creation_tokens), 0),
            "reasoning_tokens": func.coalesce(func.sum(AgentEvent.reasoning_tokens), 0),
            "cost_usd": func.sum(AgentEvent.cost_usd),
            "avg_latency_ms": func.avg(AgentEvent.latency_ms),
            "p95_latency_ms": func.percentile_cont(0.95).within_group(AgentEvent.latency_ms),
            "runs": func.count(func.distinct(AgentEvent.thread_id)),
        }

    def _metric_columns(self) -> list[Any]:
        metric_map = self._metric_map()
        return [metric_map[name].label(name) for name in _METRIC_ORDER]

    def _row_to_metrics(self, row: Any) -> dict[str, Any]:
        return {
            name: (_as_float(value) if name in _FLOAT_METRICS else _as_int(value))
            for name, value in zip(_METRIC_ORDER, row, strict=True)
        }

    def _group_column(self, group_by: AgentGroupBy, bucket: AgentBucket) -> Any:
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
        expr = self._metric_map()[sort]
        # Order NULLs as 0 for the nullable measures so unknown-cost / latency-less
        # rows sink instead of floating to the top under Postgres NULL ordering.
        if sort in _FLOAT_METRICS:
            return func.coalesce(expr, 0)
        return expr
