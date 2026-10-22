"""DB-gated round-trip for agent event capture and aggregation.

Opt-in via APEX_TEST_DATABASE_URI (asyncpg URI). The catalog and event rows use
an isolated schema that is deleted afterwards.
"""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from apex.persistence.models import AgentEvent, Base
from apex.services import usage
from apex.services.agent_analytics import (
    AgentAnalyticsRepository,
    AgentGroupBy,
    AgentOrder,
    AgentSort,
)
from apex.services.model_pricing import ensure_model, intervals
from apex.settings import get_settings

pytestmark = pytest.mark.skipif(
    not os.environ.get("APEX_TEST_DATABASE_URI"), reason="needs postgres"
)


def test_agent_event_capture_and_aggregation_round_trip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    test_uri = os.environ["APEX_TEST_DATABASE_URI"]
    monkeypatch.setenv("APEX_DATABASE__URI", test_uri)
    # These event labels deliberately exercise priced and unpriced models.
    # Authorize them in this isolated fixture without weakening production policy.
    monkeypatch.setenv("APEX_LLM__DEFAULT_MODEL", "claude-sonnet-4-20250514")
    monkeypatch.setenv("APEX_LLM__ALLOWED_MODELS", '["claude-sonnet-4-20250514", "unknown-model"]')
    get_settings.cache_clear()

    marker = f"proj-agent-{uuid4().hex[:8]}"
    app_marker = f"app-agent-{uuid4().hex[:8]}"
    thread_id = f"thread-agent-{uuid4().hex[:8]}"
    schema = f"agent_analytics_test_{uuid4().hex}"

    def isolated_engine(uri: str, **kwargs: Any) -> AsyncEngine:
        return create_async_engine(
            uri, execution_options={"schema_translate_map": {"apex": schema}}, **kwargs
        )

    # Exercise the real throwaway-engine writer while keeping catalog pricing
    # independent of other tests and administrator-maintained rates in apex.
    monkeypatch.setattr(usage, "create_async_engine", isolated_engine)
    engine = isolated_engine(test_uri, poolclass=NullPool)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def setup() -> tuple[str, str]:
        async with engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            await conn.run_sync(Base.metadata.create_all)
        async with maker() as session:
            model = await ensure_model(session, "claude-sonnet-4-20250514", seed=True)
            timeline = await intervals(session, model.active_pricing_generation_id)
            assert len(timeline) == 1
            await session.commit()
            return model.id, timeline[0].id

    async def aggregate(
        *,
        group_by: str = "stage",
        test: str | None = None,
        sort: str = "total_tokens",
        order: str = "desc",
    ) -> dict[str, Any]:
        async with maker() as session:
            repo = AgentAnalyticsRepository(session)
            return await repo.aggregate(
                window_from=datetime.now(UTC) - timedelta(hours=1),
                window_to=datetime.now(UTC) + timedelta(hours=1),
                bucket="hour",
                group_by=cast(AgentGroupBy, group_by),
                project_id=marker,
                test=test,
                sort=cast(AgentSort, sort),
                order=cast(AgentOrder, order),
            )

    async def read_reporting_event() -> AgentEvent:
        async with maker() as session:
            row = await session.scalar(
                select(AgentEvent).where(
                    AgentEvent.project_id == marker, AgentEvent.phase == "reporting"
                )
            )
            assert row is not None
            return row

    async def cleanup() -> None:
        try:
            async with engine.begin() as conn:
                await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        finally:
            await engine.dispose()

    try:
        model_id, interval_id = asyncio.run(setup())
        config = {
            "configurable": {
                "thread_id": thread_id,
                "project_id": marker,
                "app_id": app_marker,
                "model_by_phase": {
                    "reporting": "claude-sonnet-4-20250514",
                    "execution": "unknown-model",
                },
            }
        }
        usage.record_agent_event_sync(
            phase="reporting",
            status="succeeded",
            attempt=1,
            config=config,
            latency_ms=1234,
            usage={
                "input_tokens": 1000,
                "output_tokens": 100,
                "total_tokens": 1100,
                "input_token_details": {"cache_read": 50, "cache_creation": 20},
                "output_token_details": {"reasoning": 25},
                "finish_reason": "stop",
            },
        )
        # Checkpoint replay of the same phase attempt is idempotent.
        usage.record_agent_event_sync(
            phase="reporting",
            status="succeeded",
            attempt=1,
            config=config,
            latency_ms=1234,
            usage={
                "input_tokens": 1000,
                "output_tokens": 100,
                "total_tokens": 1100,
                "input_token_details": {"cache_read": 50, "cache_creation": 20},
                "output_token_details": {"reasoning": 25},
                "finish_reason": "stop",
            },
        )
        usage.record_agent_event_sync(
            phase="execution",
            status="failed",
            attempt=2,
            config=config,
            latency_ms=456,
            usage={"input_tokens": 40, "output_tokens": 10, "total_tokens": 50},
        )

        reporting = asyncio.run(read_reporting_event())
        assert reporting.model == "claude-sonnet-4-20250514"
        assert reporting.app_id == app_marker
        assert reporting.provider == "anthropic"
        assert reporting.total_tokens == 1100
        # Cached tokens are a subset of input_tokens, billed once at the cache rate
        # (not also at the full input rate): (1000-50-20)*3 + 100*15 + 50*0.30 + 20*3.75.
        assert reporting.cost_usd == Decimal("0.004380")
        assert reporting.model_catalog_id == model_id
        assert reporting.pricing_interval_id == interval_id
        assert Decimal(reporting.extra["pricing"]["input"]) == Decimal("3.00")
        assert reporting.extra["finish_reason"] == "stop"

        data = asyncio.run(aggregate())
        assert data["totals"]["events"] == 2
        assert data["totals"]["latency_samples"] == 2
        assert data["totals"]["errors"] == 1
        assert data["totals"]["total_tokens"] == 1150
        assert data["totals"]["runs"] == 1
        assert data["totals"]["cost_usd"] == 0.00438
        rows = {row["key"]: row for row in data["breakdown"]}
        assert rows["reporting"]["total_tokens"] == 1100
        assert rows["reporting"]["latency_samples"] == 1
        assert all(point["latency_samples"] == 1 for point in data["series"])
        assert rows["execution"]["errors"] == 1
        assert rows["execution"]["cost_usd"] is None

        # group_by model: the cost-bearing model vs the unknown-model split (review R2).
        model_data = asyncio.run(aggregate(group_by="model"))
        by_model = {row["model"]: row for row in model_data["breakdown"]}
        assert by_model["claude-sonnet-4-20250514"]["total_tokens"] == 1100
        assert by_model["unknown-model"]["cost_usd"] is None

        # group_by agent: one row per "{phase}.worker".
        by_agent = {row["key"] for row in asyncio.run(aggregate(group_by="agent"))["breakdown"]}
        assert by_agent == {"reporting.worker", "execution.worker"}

        # group_by date: bounded to window buckets; distinct runs counted once.
        by_date = asyncio.run(aggregate(group_by="date"))
        assert by_date["totals"]["runs"] == 1
        assert len(by_date["breakdown"]) >= 1

        # sort + order + p95 across the non-default branches (single-event phases, so
        # p95 == that event's latency: reporting 1234ms > execution 456ms).
        desc = asyncio.run(aggregate(group_by="stage", sort="p95_latency_ms", order="desc"))
        assert [row["key"] for row in desc["breakdown"]][0] == "reporting"
        assert desc["breakdown"][0]["p95_latency_ms"] is not None
        asc = asyncio.run(aggregate(group_by="stage", sort="p95_latency_ms", order="asc"))
        assert [row["key"] for row in asc["breakdown"]][0] == "execution"

        filtered = asyncio.run(aggregate(group_by="test", test=thread_id[-6:]))
        assert filtered["totals"]["events"] == 2
        assert filtered["breakdown"][0]["key"] == thread_id
    finally:
        asyncio.run(cleanup())
