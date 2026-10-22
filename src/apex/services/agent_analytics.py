"""Agent-behavior analytics aggregation over apex.agent_events."""

import math
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import ColumnElement, Select, String, case, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from apex.auth.identity import ScopeRef
from apex.domain.diagnostics import bounded_diagnostic
from apex.domain.input_limits import MAX_SCOPE_ID_CHARS
from apex.persistence.models import AgentEvent, ModelCatalogEntry
from apex.services.analytics_scope import analytics_scope_filter

AgentGroupBy = Literal["model", "stage", "agent", "date", "test", "application", "user"]
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
MAX_ANALYTICS_KEY_CHARS = 255

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
    "latency_samples",
    "priced_events",
    "unpriced_events",
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
    try:
        converted = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return converted if math.isfinite(converted) else None


def _key_string(value: Any, *, max_chars: int = MAX_ANALYTICS_KEY_CHARS) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if value is None or value == "":
        return "unknown"
    # Rows created before current write validation (or repaired directly in the
    # database) are still untrusted at this public projection boundary.  Render
    # only safe built-ins, redact credential signatures, and cap response size.
    return bounded_diagnostic(value, max_chars=max_chars)


def _thread_id_string(value: Any) -> str | None:
    return None if value is None else _key_string(value)


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def event_provider() -> Any:
    """Exact registry provenance where known, retaining unknown legacy provenance."""
    registered = (
        select(ModelCatalogEntry.provider)
        .where(ModelCatalogEntry.id == AgentEvent.model_catalog_id)
        .correlate(AgentEvent)
        .scalar_subquery()
    )
    return func.coalesce(registered, AgentEvent.provider, "")


def _optional_metadata(value: Any, max_chars: int) -> str | None:
    return None if value is None else bounded_diagnostic(value, max_chars=max_chars)


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
        consumer_id: str | None = None,
        app_id: str | None = None,
        model_id: str | None = None,
        provider: str | None = None,
        cost_basis: str = "corrected",
    ) -> dict[str, Any]:
        # Totals, ranked rows and charts must observe one published correction set.
        if (
            isinstance(self._session, AsyncSession)
            and not self._session.in_transaction()
            and self._session.get_bind().dialect.name == "postgresql"
        ):
            await self._session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        self._cost_basis = cost_basis
        filters = self._filters(
            window_from=window_from,
            window_to=window_to,
            project_id=project_id,
            visible_scopes=visible_scopes,
            models=models,
            stages=stages,
            agents=agents,
            test=test,
            status=status,
        )
        if consumer_id is not None:
            filters.append(AgentEvent.consumer_id == consumer_id)
        if app_id is not None:
            filters.append(AgentEvent.app_id == app_id)
        if model_id is not None:
            filters.append(AgentEvent.model_catalog_id == model_id)
        if provider is not None:
            filters.append(event_provider() == provider)
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
                select(func.count(func.distinct(self._model_identity()))).where(*filters)
            )
        )

        group_expr = self._group_column(group_by, bucket)
        group_col = group_expr.label("key")
        count_stmt = select(func.count()).select_from(
            select(group_col).where(*filters).group_by(group_col).subquery()
        )
        total_rows = _as_int(await self._session.scalar(count_stmt))

        sort_expr = self._sort_expression(sort, group_col)
        # Missing measurements must not tie with real zero values or displace
        # measured groups from the bounded chart population.
        ordered = (
            (sort_expr.asc() if order == "asc" else sort_expr.desc()).nulls_last(),
            group_col.asc(),
        )
        metadata = self._group_metadata_columns(group_by)
        grouped_cols = [group_col, *metrics, *metadata]
        grouped: Select[tuple[Any, ...]] = (
            select(*grouped_cols)
            .where(*filters)
            .group_by(group_col)
            .order_by(*ordered)
            .limit(limit)
            .offset(offset)
        )
        if group_by == "model":
            grouped = grouped.outerjoin(
                ModelCatalogEntry, ModelCatalogEntry.id == AgentEvent.model_catalog_id
            )
        breakdown_rows = (await self._session.execute(grouped)).all()
        # Application identities contain two independently bounded scope IDs plus
        # their length prefixes. Truncating to one label's limit changes identity
        # and breaks drill-downs for otherwise valid project/application pairs.
        key_limit = (
            2 * (MAX_SCOPE_ID_CHARS + len(str(MAX_SCOPE_ID_CHARS)) + 1)
            if group_by == "application"
            else (340 if group_by == "model" else MAX_ANALYTICS_KEY_CHARS)
        )
        breakdown = [
            {
                "key": _key_string(row[0], max_chars=key_limit),
                "thread_id": _thread_id_string(row[0]) if group_by == "test" else None,
            }
            | self._row_to_metrics(row[1 : 1 + len(_METRIC_ORDER)])
            | self._render_group_metadata(group_by, row[1 + len(_METRIC_ORDER) :])
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
        series_statement = (
            select(bucket_col, group_col, *metrics, *metadata)
            .where(*series_filters)
            .group_by(bucket_col, group_col)
            .order_by(bucket_col, group_col)
        )
        if group_by == "model":
            series_statement = series_statement.outerjoin(
                ModelCatalogEntry, ModelCatalogEntry.id == AgentEvent.model_catalog_id
            )
        series_rows = (await self._session.execute(series_statement)).all()
        series = [
            {"bucket_start": row[0], "key": _key_string(row[1], max_chars=key_limit)}
            | self._row_to_metrics(row[2 : 2 + len(_METRIC_ORDER)])
            | self._render_group_metadata(group_by, row[2 + len(_METRIC_ORDER) :])
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
        visible_scopes: tuple[ScopeRef, ...] | None,
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
        scope_filter = analytics_scope_filter(AgentEvent, visible_scopes)
        if scope_filter is not None:
            filters.append(scope_filter)
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
        from apex.services.model_pricing import effective_cost

        cost = (
            AgentEvent.cost_usd
            if getattr(self, "_cost_basis", "corrected") == "original"
            else effective_cost()
        )
        return {
            "events": func.count(),
            "errors": func.sum(case((AgentEvent.status == "error", 1), else_=0)),
            "input_tokens": func.coalesce(func.sum(AgentEvent.input_tokens), 0),
            "output_tokens": func.coalesce(func.sum(AgentEvent.output_tokens), 0),
            "total_tokens": func.coalesce(func.sum(AgentEvent.total_tokens), 0),
            "cache_read_tokens": func.coalesce(func.sum(AgentEvent.cache_read_tokens), 0),
            "cache_creation_tokens": func.coalesce(func.sum(AgentEvent.cache_creation_tokens), 0),
            "reasoning_tokens": func.coalesce(func.sum(AgentEvent.reasoning_tokens), 0),
            "cost_usd": func.sum(cost),
            "priced_events": func.count(cost),
            "unpriced_events": func.count() - func.count(cost),
            "avg_latency_ms": func.avg(AgentEvent.latency_ms),
            "latency_samples": func.count(AgentEvent.latency_ms),
            "p95_latency_ms": func.percentile_cont(0.95).within_group(AgentEvent.latency_ms),
            "runs": func.count(func.distinct(AgentEvent.thread_id)),
        }

    def _metric_columns(self) -> list[Any]:
        metric_map = self._metric_map()
        return [metric_map[name].label(name) for name in _METRIC_ORDER]

    def _row_to_metrics(self, row: Any) -> dict[str, Any]:
        result = {
            name: (_as_float(value) if name in _FLOAT_METRICS else _as_int(value))
            for name, value in zip(_METRIC_ORDER, row, strict=True)
        }
        result["cost_complete"] = result["unpriced_events"] == 0
        return result

    def _group_column(self, group_by: AgentGroupBy, bucket: AgentBucket) -> Any:
        if group_by == "user":
            return func.coalesce(AgentEvent.consumer_id, "unknown")
        if group_by == "application":
            # Length prefixes make keys collision-free even with delimiters in IDs.
            project = func.coalesce(AgentEvent.project_id, "")
            app = func.coalesce(AgentEvent.app_id, "")
            return (
                cast(func.length(project), String)
                + ":"
                + project
                + cast(func.length(app), String)
                + ":"
                + app
            )
        if group_by == "model":
            return func.coalesce(self._model_identity(), "unknown")
        if group_by == "stage":
            return AgentEvent.phase
        if group_by == "agent":
            return AgentEvent.agent_name
        if group_by == "test":
            return func.coalesce(AgentEvent.thread_id, "unknown")
        return func.date_trunc(bucket, AgentEvent.at)

    def _model_identity(self) -> Any:
        provider = func.coalesce(AgentEvent.provider, "")
        model = AgentEvent.model
        legacy = (
            "legacy:"
            + cast(func.length(provider), String)
            + ":"
            + provider
            + cast(func.length(model), String)
            + ":"
            + model
        )
        return func.coalesce(AgentEvent.model_catalog_id, legacy)

    def _group_metadata_columns(self, group_by: AgentGroupBy) -> list[Any]:
        if group_by == "model":
            return [
                func.max(AgentEvent.model_catalog_id),
                func.max(func.coalesce(ModelCatalogEntry.model_key, AgentEvent.model)),
                func.max(func.coalesce(ModelCatalogEntry.provider, AgentEvent.provider)),
                func.max(ModelCatalogEntry.display_name),
                func.max(case((ModelCatalogEntry.archived_at.is_not(None), 1), else_=0)),
            ]
        if group_by == "application":
            return [func.max(AgentEvent.project_id), func.max(AgentEvent.app_id)]
        if group_by == "user":
            return [func.max(AgentEvent.consumer_name_snapshot), func.max(AgentEvent.consumer_id)]
        return []

    def _render_group_metadata(self, group_by: AgentGroupBy, values: Any) -> dict[str, Any]:
        if group_by == "model":
            model_id, model, provider, display_name, archived = values
            label = display_name or model or "Unknown model"
            if provider:
                label = f"{provider} / {label}"
            return {
                "model_id": _optional_metadata(model_id, 32),
                "model": _optional_metadata(model, MAX_ANALYTICS_KEY_CHARS),
                "provider": _optional_metadata(provider, 64),
                "label": _key_string(label, max_chars=340),
                "archived": bool(archived),
            }
        if group_by == "application":
            project, app = values
            return {
                "project_id": _optional_metadata(project, MAX_SCOPE_ID_CHARS),
                "app_id": _optional_metadata(app, MAX_SCOPE_ID_CHARS),
                "label": _key_string(
                    f"{project or 'Unknown project'} / {app or 'Unknown application'}",
                    max_chars=520,
                ),
            }
        if group_by == "user":
            name, consumer_id = values
            return {"label": _key_string(name or consumer_id or "Unattributed")}
        return {}

    def _sort_expression(self, sort: AgentSort, group_col: Any) -> Any:
        if sort == "key":
            return group_col
        return self._metric_map()[sort]
