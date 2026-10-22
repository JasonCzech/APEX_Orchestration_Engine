"""Unit coverage for the SQL aggregation repository without requiring Postgres.

The database-gated round-trip remains the integration proof for SQL semantics;
these tests exercise result shaping, query selection, filtering, sorting, and
cardinality bounding deterministically in every CI job.
"""

from collections import deque
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from apex.auth.identity import ScopeRef
from apex.persistence.models import AgentEvent, Base
from apex.services.agent_analytics import (
    MAX_ANALYTICS_KEY_CHARS,
    SERIES_KEY_CAP,
    AgentAnalyticsRepository,
    AgentOrder,
    AgentSort,
    _as_float,
    _escape_like,
    _key_string,
)

METRICS = (11, 2, 100, 40, 140, 5, 6, 7, Decimal("1.25"), 12.5, 18.75, 3, 4, 11, 0)


class _Rows:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def one(self) -> tuple[Any, ...]:
        assert len(self._rows) == 1
        return self._rows[0]

    def all(self) -> list[tuple[Any, ...]]:
        return self._rows


class _Session:
    def __init__(
        self,
        *,
        executes: list[list[tuple[Any, ...]]],
        scalars: list[Any],
    ) -> None:
        self._executes = deque(executes)
        self._scalars = deque(scalars)
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> _Rows:
        self.statements.append(statement)
        return _Rows(self._executes.popleft())

    async def scalar(self, statement: Any) -> Any:
        self.statements.append(statement)
        return self._scalars.popleft()


@pytest.mark.asyncio
async def test_aggregate_shapes_filtered_test_breakdown_and_bounded_series() -> None:
    bucket_start = datetime(2026, 7, 1, tzinfo=UTC)
    session = _Session(
        executes=[
            [METRICS],
            [("run-2", *METRICS), (None, *METRICS)],
            [("run-2",), (None,)],
            [(bucket_start, "run-2", *METRICS), (bucket_start, None, *METRICS)],
        ],
        scalars=[4, 2, 2],
    )
    repository = AgentAnalyticsRepository(cast(AsyncSession, session))
    start = datetime(2026, 7, 1, tzinfo=UTC)

    result = await repository.aggregate(
        window_from=start,
        window_to=start + timedelta(days=1),
        bucket="hour",
        group_by="test",
        project_id="project-a",
        visible_scopes=(ScopeRef(project_id="project-a", app_id="app-a"),),
        models=("model-a",),
        stages=("execution",),
        agents=("execution.worker",),
        test=r"run%_\\needle",
        status="error",
        sort="p95_latency_ms",
        order="asc",
        limit=7,
        offset=3,
    )

    assert result["totals"] == {
        "events": 11,
        "priced_events": 11,
        "unpriced_events": 0,
        "cost_complete": True,
        "errors": 2,
        "input_tokens": 100,
        "output_tokens": 40,
        "total_tokens": 140,
        "cache_read_tokens": 5,
        "cache_creation_tokens": 6,
        "reasoning_tokens": 7,
        "cost_usd": 1.25,
        "avg_latency_ms": 12.5,
        "latency_samples": 4,
        "p95_latency_ms": 18.75,
        "runs": 3,
        "agents": 4,
        "models": 2,
    }
    assert result["breakdown"][0]["thread_id"] == "run-2"
    assert result["breakdown"][1]["key"] == "unknown"
    assert result["breakdown"][0]["latency_samples"] == 4
    assert result["series"][0]["latency_samples"] == 4
    assert result["series"][0]["bucket_start"] == bucket_start
    assert result["series"][1]["key"] == "unknown"
    assert result["page"] == {"limit": 7, "offset": 3, "total": 2}
    assert not session._executes
    assert not session._scalars

    sql = "\n".join(str(statement) for statement in session.statements)
    assert "agent_events.project_id" in sql
    assert "agent_events.model IN" in sql
    assert "agent_events.phase IN" in sql
    assert "agent_events.agent_name IN" in sql
    assert "agent_events.thread_id" in sql
    assert "agent_events.status" in sql
    assert f"LIMIT :param_{len(session.statements) + 1}" not in sql  # compiler stays parameterized
    # One query fetches only the bounded key set before the series query.
    assert any(
        str(SERIES_KEY_CAP) in str(statement.compile(compile_kwargs={"literal_binds": True}))
        for statement in session.statements
    )


@pytest.mark.asyncio
async def test_date_aggregate_skips_key_prefetch_and_handles_empty_nullable_metrics() -> None:
    start = datetime(2026, 7, 1, tzinfo=UTC)
    empty_metrics = (0, None, None, 0, 0, 0, 0, 0, None, None, float("inf"), None, 0, 0, 0)
    session = _Session(
        executes=[
            [empty_metrics],
            [(start, *empty_metrics)],
            [(start, start, *empty_metrics)],
        ],
        scalars=[None, 0, None],
    )
    repository = AgentAnalyticsRepository(cast(AsyncSession, session))

    result = await repository.aggregate(
        window_from=start,
        window_to=start + timedelta(days=2),
        bucket="day",
        group_by="date",
        sort="key",
        order="desc",
    )

    assert result["totals"]["latency_samples"] == 0
    assert result["totals"]["events"] == 0
    assert result["totals"]["cost_usd"] is None
    assert result["totals"]["p95_latency_ms"] is None
    assert result["totals"]["agents"] == 0
    assert result["breakdown"][0]["key"] == start.isoformat()
    assert result["breakdown"][0]["thread_id"] is None
    assert result["series"][0]["key"] == start.isoformat()
    assert len(session.statements) == 6


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, None), ("bad", None), (object(), None), (Decimal("2.5"), 2.5)],
)
def test_float_projection_rejects_invalid_legacy_values(value: Any, expected: float | None) -> None:
    assert _as_float(value) == expected


def test_repository_helpers_preserve_safe_display_and_like_semantics() -> None:
    assert _key_string("") == "unknown"
    assert _key_string(None) == "unknown"
    assert _key_string(42) == "42"
    assert _escape_like(r"a%b_c\d") == r"a\%b\_c\\d"

    repository = AgentAnalyticsRepository(cast(AsyncSession, object()))
    assert repository._group_column("model", "day") is not None
    assert repository._group_column("stage", "day") is not None
    assert repository._group_column("agent", "day") is not None
    assert repository._group_column("test", "day") is not None
    assert repository._group_column("date", "hour") is not None
    group = repository._group_column("model", "day")
    assert repository._sort_expression("key", group) is group
    assert repository._sort_expression("events", group) is not None
    assert repository._sort_expression("cost_usd", group) is not None


def test_analytics_key_projection_redacts_credentials_and_bounds_legacy_values() -> None:
    credential = "ghp_" + "A" * 24

    assert _key_string(credential) == "[REDACTED]"
    assert credential not in _key_string(credential)
    assert _key_string("x" * (MAX_ANALYTICS_KEY_CHARS + 100)) == "x" * MAX_ANALYTICS_KEY_CHARS


@pytest.mark.parametrize(
    ("latencies", "sample_count", "average"),
    [([], 0, None), ([None, None], 0, None), ([None, 0, 100], 2, 50.0)],
)
def test_latency_sample_count_uses_the_same_non_null_observations_as_average(
    latencies: list[int | None], sample_count: int, average: float | None
) -> None:
    engine = create_engine("sqlite://").execution_options(schema_translate_map={"apex": None})
    try:
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            session.add_all(
                [
                    AgentEvent(
                        phase="execution", agent_name="worker", status="ok", latency_ms=value
                    )
                    for value in latencies
                ]
            )
            session.flush()
            metrics = AgentAnalyticsRepository(cast(AsyncSession, session))._metric_map()
            row = session.execute(
                select(metrics["events"], metrics["latency_samples"], metrics["avg_latency_ms"])
            ).one()
            assert tuple(row) == (len(latencies), sample_count, average)
    finally:
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("sort", ["cost_usd", "avg_latency_ms"])
@pytest.mark.parametrize("order", ["asc", "desc"])
async def test_bounded_series_keeps_measured_zero_before_missing_groups(
    sort: AgentSort, order: AgentOrder
) -> None:
    start = datetime(2026, 7, 1, tzinfo=UTC)
    captured = _Session(executes=[[METRICS], [], [], []], scalars=[0, 0, 15])
    await AgentAnalyticsRepository(cast(AsyncSession, captured)).aggregate(
        window_from=start,
        window_to=start + timedelta(days=1),
        bucket="day",
        group_by="model",
        sort=sort,
        order=order,
    )
    # Execute the repository's actual bounded-key query against real null/zero
    # rows. Unlike percentile_cont, SUM/AVG have matching SQLite semantics.
    key_query = captured.statements[-2]
    engine = create_engine("sqlite://").execution_options(schema_translate_map={"apex": None})
    try:
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            session.add_all(
                [
                    AgentEvent(
                        at=start,
                        phase="execution",
                        agent_name="worker",
                        status="ok",
                        model=f"a-missing-{index:02}",
                    )
                    for index in range(SERIES_KEY_CAP + 1)
                ]
                + [
                    AgentEvent(
                        at=start,
                        phase="execution",
                        agent_name="worker",
                        status="ok",
                        model="z-zero",
                        latency_ms=0,
                        cost_usd=Decimal(0),
                    ),
                    AgentEvent(
                        at=start,
                        phase="execution",
                        agent_name="worker",
                        status="ok",
                        model="z-positive",
                        latency_ms=100,
                        cost_usd=Decimal(1),
                    ),
                ]
            )
            session.flush()
            keys = list(session.scalars(key_query))
            names = ["z-zero", "z-positive"] if order == "asc" else ["z-positive", "z-zero"]
            assert keys[:2] == [f"legacy:0:{len(name)}:{name}" for name in names]
            assert len(keys) == SERIES_KEY_CAP
    finally:
        engine.dispose()
