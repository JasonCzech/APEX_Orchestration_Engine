"""DB-gated round-trip for agent event capture and aggregation.

Opt-in via APEX_TEST_DATABASE_URI (asyncpg URI). Rows are isolated by a unique
project marker and deleted afterwards.
"""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import Table, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from apex.persistence.models import AgentEvent
from apex.services import usage
from apex.services.agent_analytics import AgentAnalyticsRepository, AgentGroupBy
from apex.settings import get_settings

pytestmark = pytest.mark.skipif(
    not os.environ.get("APEX_TEST_DATABASE_URI"), reason="needs postgres"
)


def test_agent_event_capture_and_aggregation_round_trip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    test_uri = os.environ["APEX_TEST_DATABASE_URI"]
    monkeypatch.setenv("APEX_DATABASE__URI", test_uri)
    get_settings.cache_clear()

    marker = f"proj-agent-{uuid4().hex[:8]}"
    thread_id = f"thread-agent-{uuid4().hex[:8]}"
    engine = create_async_engine(test_uri, poolclass=NullPool)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def setup() -> None:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE SCHEMA IF NOT EXISTS apex"))
            await conn.run_sync(
                lambda sync_conn: cast(Table, AgentEvent.__table__).create(
                    sync_conn, checkfirst=True
                )
            )

    async def aggregate(*, group_by: str = "stage", test: str | None = None) -> dict[str, Any]:
        async with maker() as session:
            repo = AgentAnalyticsRepository(session)
            return await repo.aggregate(
                window_from=datetime.now(UTC) - timedelta(hours=1),
                window_to=datetime.now(UTC) + timedelta(hours=1),
                bucket="hour",
                group_by=cast(AgentGroupBy, group_by),
                project_id=marker,
                test=test,
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
            async with maker() as session:
                rows = await session.scalars(
                    select(AgentEvent).where(AgentEvent.project_id == marker)
                )
                for row in rows:
                    await session.delete(row)
                await session.commit()
        finally:
            await engine.dispose()

    asyncio.run(setup())
    try:
        config = {
            "configurable": {
                "thread_id": thread_id,
                "project_id": marker,
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
        assert reporting.provider == "anthropic"
        assert reporting.total_tokens == 1100
        assert reporting.cost_usd == Decimal("0.004590")
        assert reporting.extra["pricing"]["input"] == "3.00"
        assert reporting.extra["finish_reason"] == "stop"

        data = asyncio.run(aggregate())
        assert data["totals"]["events"] == 2
        assert data["totals"]["errors"] == 1
        assert data["totals"]["total_tokens"] == 1150
        assert data["totals"]["runs"] == 1
        assert data["totals"]["cost_usd"] == 0.00459
        rows = {row["key"]: row for row in data["breakdown"]}
        assert rows["reporting"]["total_tokens"] == 1100
        assert rows["execution"]["errors"] == 1
        assert rows["execution"]["cost_usd"] is None

        filtered = asyncio.run(aggregate(group_by="test", test=thread_id[-6:]))
        assert filtered["totals"]["events"] == 2
        assert filtered["breakdown"][0]["key"] == thread_id
    finally:
        asyncio.run(cleanup())
