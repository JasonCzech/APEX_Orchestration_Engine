"""Regression coverage for usage review findings, including private detail reads."""

import csv
import io
import os
from collections import deque
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apex.app.dependencies import get_current_identity
from apex.auth.identity import ConsumerIdentity, ConsumerType, Role
from apex.persistence.db import get_session
from apex.persistence.models import (
    AgentEvent,
    Base,
    ModelCatalogEntry,
    ModelPriceInterval,
    ModelPricingGeneration,
    PricingCorrectionJob,
    UsageCostAdjustment,
)
from apex.routers.usage_history import router
from apex.services.agent_analytics import AgentAnalyticsRepository
from apex.services.model_pricing import ensure_model, price_event
from apex.settings import get_settings

ADMIN = ConsumerIdentity(
    consumer_id="admin", name="Admin", consumer_type=ConsumerType.DASHBOARD, role=Role.ADMIN
)


@pytest_asyncio.fixture(
    params=["sqlite"] + (["postgres"] if os.environ.get("APEX_PRICING_TEST_URI") else [])
)
async def history_db(request):
    schema = "usage_review_" + uuid4().hex if request.param == "postgres" else None
    uri = os.environ["APEX_PRICING_TEST_URI"] if schema else "sqlite+aiosqlite://"
    engine = create_async_engine(uri, execution_options={"schema_translate_map": {"apex": schema}})
    async with engine.begin() as conn:
        if schema:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        await conn.run_sync(Base.metadata.create_all)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            yield session
    finally:
        if schema:
            async with engine.begin() as conn:
                await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await engine.dispose()


def history_app(session: AsyncSession, *, show_cost: bool = True) -> FastAPI:
    app = FastAPI()
    app.include_router(router, prefix="/v1")

    async def current_session():
        yield session

    app.dependency_overrides[get_session] = current_session
    app.dependency_overrides[get_current_identity] = lambda: ADMIN
    app.dependency_overrides[get_settings] = lambda: SimpleNamespace(
        analytics_cost_visible=show_cost
    )
    return app


async def corrected_event(session: AsyncSession) -> AgentEvent:
    model = await ensure_model(session, "gpt-4o", seed=True)
    now = datetime.now(UTC)
    values = {
        "at": now - timedelta(minutes=1),
        "model": "gpt-4o",
        "provider": "openai",
        "consumer_id": "alice",
        "project_id": "project",
        "app_id": "application",
        "phase": "execution",
        "agent_name": "worker",
        "status": "ok",
        "input_tokens": 1_000_000,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "extra": {},
    }
    await price_event(session, values)
    event = AgentEvent(**values)
    generation = ModelPricingGeneration(
        model_id=model.id,
        number=2,
        source="historical_correction",
        reason="Correct rate",
        published_at=now,
    )
    session.add_all([event, generation])
    await session.flush()
    interval = ModelPriceInterval(
        generation_id=generation.id,
        effective_from=now - timedelta(days=1),
        input=Decimal("4"),
        output=Decimal("8"),
        cache_read=Decimal("1"),
        cache_creation=Decimal("4"),
    )
    job = PricingCorrectionJob(
        model_id=model.id,
        generation_id=generation.id,
        base_generation_id=model.active_pricing_generation_id,
        expected_version=model.version,
        effective_from=now - timedelta(days=1),
        effective_to=now,
        actor_id=ADMIN.consumer_id,
        reason="Correct rate",
        idempotency_key="test-correction",
        payload_hash="hash",
        state="succeeded",
    )
    session.add_all([interval, job])
    await session.flush()
    session.add(
        UsageCostAdjustment(
            event_id=event.id,
            generation_id=generation.id,
            interval_id=interval.id,
            job_id=job.id,
            cost_usd=Decimal("4"),
        )
    )
    model.active_pricing_generation_id = generation.id
    await session.commit()
    return event


@pytest.mark.asyncio
async def test_export_respects_selected_original_and_corrected_cost_basis(history_db):
    await corrected_event(history_db)
    app = history_app(history_db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for basis, expected in [("original", Decimal("2.5")), ("corrected", Decimal("4"))]:
            response = await client.get(f"/v1/analytics/agents/export?cost_basis={basis}")
            assert response.status_code == 200
            row = next(csv.DictReader(io.StringIO(response.text)))
            assert Decimal(row["original_cost_usd"]) == Decimal("2.5")
            assert Decimal(row["effective_cost_usd"]) == expected


@pytest.mark.asyncio
async def test_administrator_personal_detail_cannot_cross_into_authorized_view(history_db):
    event = await corrected_event(history_db)
    app = history_app(history_db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        path = f"/v1/analytics/agents/events/{event.id}"
        assert (await client.get(path + "?view=mine")).status_code == 404
        authorized = await client.get(path + "?view=authorized")
        assert authorized.status_code == 200
        assert authorized.json()["cost_visible"] is True
        assert Decimal(authorized.json()["effective_cost_usd"]) == Decimal("4")
        correction = authorized.json()["corrections"][0]
        assert Decimal(correction["pricing"]["input"]) == Decimal("4")
        assert correction["pricing"]["currency"] == "USD"
        assert correction["pricing"]["unit"] == "million_tokens"


@pytest.mark.asyncio
async def test_disabled_cost_visibility_hides_detail_and_export_provenance(history_db):
    event = await corrected_event(history_db)
    app = history_app(history_db, show_cost=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        detail = (await client.get(f"/v1/analytics/agents/events/{event.id}")).json()
        assert detail["cost_visible"] is False
        assert detail["original_cost_usd"] is None
        assert detail["effective_cost_usd"] is None
        assert detail["pricing_interval_id"] is None
        assert detail["original_pricing"] is None
        assert detail["corrections"] == []
        response = await client.get("/v1/analytics/agents/export")
        row = next(csv.DictReader(io.StringIO(response.text)))
        assert row["original_cost_usd"] == row["effective_cost_usd"] == ""
        assert row["pricing_interval_id"] == ""


@pytest.mark.asyncio
async def test_history_end_only_filter_defaults_to_preceding_seven_days(history_db):
    end = datetime.now(UTC) - timedelta(days=40)
    event = AgentEvent(
        at=end - timedelta(days=2),
        consumer_id=ADMIN.consumer_id,
        phase="execution",
        agent_name="worker",
        status="ok",
    )
    history_db.add(event)
    await history_db.commit()
    app = history_app(history_db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/analytics/agents/events", params={"to": end.isoformat()})
        assert response.status_code == 200
        assert [row["id"] for row in response.json()["items"]] == [event.id]
        invalid = await client.get(
            "/v1/analytics/agents/events", params={"to": "0001-01-01T00:00:00Z"}
        )
        assert invalid.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("parameter", ["model_id", "model", "test", "consumer_id", "q"])
async def test_history_filters_reject_nul_before_database_access(history_db, parameter):
    app = history_app(history_db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/analytics/agents/facets", params={parameter: "bad\x00id"})
        assert response.status_code == 422


@pytest.mark.asyncio
async def test_application_breakdown_and_series_preserve_complete_pair_identity():
    project, application = "p" * 255, "a" * 255
    key = f"{len(project)}:{project}{len(application)}:{application}"
    now = datetime.now(UTC)
    metrics = (1, 0, 0, 0, 0, 0, 0, 0, Decimal("0"), None, None, 1, 0, 1, 0)

    class Rows:
        def __init__(self, rows):
            self.rows = rows

        def one(self):
            return self.rows[0]

        def all(self):
            return self.rows

    class QuerySession:
        def __init__(self):
            self.results = deque(
                [
                    [metrics],
                    [(key, *metrics, project, application)],
                    [(key,)],
                    [(now, key, *metrics, project, application)],
                ]
            )
            self.counts = deque([1, 1, 1])

        async def execute(self, statement: Any):
            return Rows(self.results.popleft())

        async def scalar(self, statement: Any):
            return self.counts.popleft()

    result = await AgentAnalyticsRepository(cast(AsyncSession, QuerySession())).aggregate(
        window_from=now - timedelta(days=1),
        window_to=now,
        bucket="day",
        group_by="application",
    )
    assert result["breakdown"][0]["key"] == key
    assert result["series"][0]["key"] == key


@pytest.mark.asyncio
async def test_model_groups_distinguish_exact_provider_identity(history_db):
    now = datetime.now(UTC)
    registered = [
        await ensure_model(history_db, "same-name", provider)
        for provider in ("provider-one", "provider-two")
    ]
    registered[1].archived_at = now
    for model in registered:
        history_db.add(
            AgentEvent(
                at=now - timedelta(minutes=1),
                model_catalog_id=model.id,
                model=model.model_key,
                # Backfill retains old event text, which may lack provider provenance.
                provider=None if model is registered[0] else model.provider,
                consumer_id="alice",
                phase="execution",
                agent_name="worker",
                status="ok",
            )
        )
    for provider in ("provider-one", "provider-two", None):
        history_db.add(
            AgentEvent(
                at=now - timedelta(minutes=1),
                model="legacy-name",
                provider=provider,
                consumer_id="alice",
                phase="execution",
                agent_name="worker",
                status="ok",
            )
        )
    await history_db.commit()
    repo = AgentAnalyticsRepository(history_db)
    identity = repo._group_column("model", "day")
    distinct_count = await history_db.scalar(
        select(func.count(func.distinct(repo._model_identity())))
    )
    assert distinct_count == 5
    groups = (
        await history_db.execute(
            select(identity, *repo._group_metadata_columns("model"))
            .outerjoin(ModelCatalogEntry, ModelCatalogEntry.id == AgentEvent.model_catalog_id)
            .group_by(identity)
        )
    ).all()
    assert len(groups) == 5
    metadata = {row[0]: repo._render_group_metadata("model", row[1:]) for row in groups}
    assert metadata[registered[1].id]["archived"] is True
    assert metadata[registered[0].id]["provider"] == "provider-one"
    assert metadata[registered[0].id]["model"] == "same-name"
    assert len({item["label"] for item in metadata.values()}) == 5
    if history_db.get_bind().dialect.name == "postgresql":
        result = await repo.aggregate(
            window_from=now - timedelta(days=1), window_to=now, bucket="day", group_by="model"
        )
        assert result["totals"]["models"] == result["page"]["total"] == 5
        assert {row["key"] for row in result["series"]} == set(metadata)
        filtered = await repo.aggregate(
            window_from=now - timedelta(days=1),
            window_to=now,
            bucket="day",
            group_by="model",
            provider="provider-one",
        )
        assert filtered["totals"]["models"] == 2
        assert all(row["provider"] == "provider-one" for row in filtered["breakdown"])

    app = history_app(history_db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/v1/analytics/agents/events",
            params={"model": "legacy-name", "provider": "provider-two"},
        )
        assert response.status_code == 200
        assert len(response.json()["items"]) == 1
        assert response.json()["items"][0]["provider"] == "provider-two"
        backfilled = await client.get(
            "/v1/analytics/agents/events", params={"model": "same-name", "provider": "provider-one"}
        )
        assert backfilled.status_code == 200
        assert len(backfilled.json()["items"]) == 1
        assert backfilled.json()["items"][0]["model_catalog_id"] == registered[0].id
        unknown_provider = await client.get(
            "/v1/analytics/agents/events", params={"model": "legacy-name", "provider": ""}
        )
        assert unknown_provider.status_code == 200
        assert len(unknown_provider.json()["items"]) == 1
        assert unknown_provider.json()["items"][0]["provider"] is None


def test_group_metadata_does_not_expose_credentials_from_legacy_text():
    repo = AgentAnalyticsRepository(cast(AsyncSession, object()))
    credential = "ghp_" + "A" * 24
    model = repo._render_group_metadata("model", (None, credential, credential, None, False))
    assert model["model"] == model["provider"] == "[REDACTED]"
    assert credential not in model["label"]
    app = repo._render_group_metadata("application", (credential, credential))
    assert app["project_id"] == app["app_id"] == "[REDACTED]"
    assert credential not in app["label"]
    user = repo._render_group_metadata("user", (credential, credential))
    assert user["label"] == "[REDACTED]"


@pytest.mark.asyncio
async def test_history_detail_and_export_redact_unsafe_legacy_labels(history_db):
    credential = "ghp_" + "A" * 24
    event = AgentEvent(
        at=datetime.now(UTC) - timedelta(minutes=1),
        consumer_id=ADMIN.consumer_id,
        consumer_name_snapshot=credential,
        model=credential,
        provider=credential,
        phase="execution",
        agent_name="worker",
        status="ok",
        extra={"pricing": {"input": credential}},
    )
    history_db.add(event)
    await history_db.commit()
    app = history_app(history_db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for path in ["events", f"events/{event.id}", "export"]:
            response = await client.get(f"/v1/analytics/agents/{path}")
            assert response.status_code == 200
            assert credential not in response.text
        detail = (await client.get(f"/v1/analytics/agents/events/{event.id}")).json()
        assert detail["consumer_name_snapshot"] == "[REDACTED]"
        assert detail["original_pricing"] is None
