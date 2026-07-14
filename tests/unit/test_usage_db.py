"""DB-gated round-trip: middleware emission + graph bridge -> real aggregation.

Opt-in via APEX_TEST_DATABASE_URI (asyncpg URI, like the other *_db tests). The
table is created checkfirst so the test also passes before migration 0006 is
applied to the test database. Events are isolated by a unique project marker
and deleted afterwards.
"""

import asyncio
import os
import time
from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Table, func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from apex.app.http import app
from apex.persistence.db import get_session
from apex.persistence.models import UsageEvent
from apex.services import usage
from apex.settings import get_settings

pytestmark = pytest.mark.skipif(
    not os.environ.get("APEX_TEST_DATABASE_URI"), reason="needs postgres"
)

DEV_KEY = "usage-db-dev-key"


def test_usage_emission_and_aggregation_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    test_uri = os.environ["APEX_TEST_DATABASE_URI"]
    # Point the best-effort writer (and identity resolution) at the real test DB,
    # overriding conftest's unreachable hermetic URI.
    monkeypatch.setenv("APEX_DATABASE__URI", test_uri)
    monkeypatch.setenv("APEX_AUTH__DEV_API_KEY", DEV_KEY)
    get_settings.cache_clear()

    marker = f"proj-usage-{uuid4().hex[:8]}"
    app_marker = f"app-usage-{uuid4().hex[:8]}"
    headers = {"x-api-key": DEV_KEY}
    # NullPool: connections never outlive one operation, so the engine can be
    # shared across asyncio.run calls and the TestClient's portal loop.
    engine = create_async_engine(test_uri, poolclass=NullPool)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def setup() -> None:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE SCHEMA IF NOT EXISTS apex"))
            await conn.run_sync(
                lambda sync_conn: cast(Table, UsageEvent.__table__).create(
                    sync_conn, checkfirst=True
                )
            )

    async def count_marker_rows() -> int:
        async with maker() as session:
            value = await session.scalar(
                select(func.count()).select_from(UsageEvent).where(UsageEvent.project_id == marker)
            )
            return int(value or 0)

    async def marker_app_ids() -> set[str | None]:
        async with maker() as session:
            values = await session.scalars(
                select(UsageEvent.app_id).where(UsageEvent.project_id == marker)
            )
            return set(values)

    async def cleanup() -> None:
        try:
            async with maker() as session:
                rows = await session.scalars(
                    select(UsageEvent).where(UsageEvent.project_id == marker)
                )
                for row in rows:
                    await session.delete(row)
                await session.commit()
        finally:
            await engine.dispose()

    async def override_session() -> Any:
        async with maker() as session:
            yield session

    asyncio.run(setup())
    app.dependency_overrides[get_session] = override_session
    try:
        with TestClient(app) as client:
            # 1) /v1 middleware emission (project attributed via ?project=).
            response = client.get(
                "/v1/system/info",
                params={"project": marker, "app": app_marker},
                headers=headers,
            )
            assert response.status_code == 200

            # 2) Graph-side events through the real sync bridge.
            config = {
                "configurable": {
                    "thread_id": "t-usage-it",
                    "project_id": marker,
                    "app_id": app_marker,
                }
            }
            usage.record_phase_usage_sync("execution", "succeeded", config, attempt=1)
            usage.record_phase_usage_sync("execution", "succeeded", config, attempt=1)
            usage.record_phase_usage_sync("execution", "failed", config, attempt=2)
            usage.record_phase_usage_sync("execution", "failed", config, attempt=2)

            # The middleware write is fire-and-forget on the portal loop: poll.
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and asyncio.run(count_marker_rows()) < 3:
                time.sleep(0.05)
            assert asyncio.run(count_marker_rows()) == 3
            assert asyncio.run(marker_app_ids()) == {app_marker}

            # 3) Real SQL aggregation through the live router.
            response = client.get(
                "/v1/analytics/usage",
                params={"project": marker, "bucket": "hour"},
                headers=headers,
            )
            assert response.status_code == 200
            body = response.json()
            assert body["totals"]["events"] == 3
            assert body["totals"]["errors"] == 1  # the failed phase
            assert body["totals"]["by_surface"] == {"v1": 1, "graph": 2}
            assert body["runs"] == {"phases_succeeded": 1, "phases_failed": 1}
            actions = {row["action"]: row["count"] for row in body["top_actions"]}
            assert actions == {
                "getSystemInfo": 1,
                "phase:execution:succeeded": 1,
                "phase:execution:failed": 1,
            }
            assert sum(bucket["events"] for bucket in body["buckets"]) == 3
            assert body["window"]["bucket"] == "hour"
    finally:
        app.dependency_overrides.pop(get_session, None)
        asyncio.run(cleanup())
