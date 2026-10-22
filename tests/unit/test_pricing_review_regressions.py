"""Regressions found while reviewing retained pricing and concurrent corrections."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import pytest
from fastapi import HTTPException
from sqlalchemy import Table, func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from apex.persistence.models import AgentEvent, ModelCatalogEntry, UsageCostAdjustment
from apex.routers.model_pricing import CorrectionWrite, correct, preview
from apex.services.model_pricing import (
    ensure_model,
    price_event,
    process_correction,
    reconcile_models,
)
from apex.settings import ApexSettings
from tests.unit.test_model_pricing_history import ADMIN, RATES, event_values
from tests.unit.test_model_pricing_history import session as pricing_session

session = pricing_session


def correction_body(model, at, key="review"):
    return CorrectionWrite(
        expected_version=model.version,
        reason="Correct retained history",
        idempotency_key=key,
        rates=RATES,
        effective_from=at - timedelta(hours=1),
        effective_to=at + timedelta(hours=1),
    )


@pytest.mark.asyncio
async def test_confirmation_retains_exact_reviewed_preview_after_late_arrivals(session):
    model = await ensure_model(session, "gpt-4o", seed=True)
    at = datetime.now(UTC) - timedelta(days=2)
    first = event_values(at)
    await price_event(session, first)
    session.add(AgentEvent(**first))
    await session.commit()
    body = correction_body(model, at)
    reviewed = await preview(model.id, body, ADMIN, session)
    body.preview_token = reviewed["preview_token"]
    late = event_values(at)
    await price_event(session, late)
    session.add(AgentEvent(**late))
    await session.commit()
    job = await correct(model.id, body, ADMIN, session)
    assert job["preview_totals"]["events"] == 1
    assert job["preview_totals"]["before_cost_usd"] == reviewed["before_cost_usd"]
    assert job["preview_totals"]["calculated_at"] == reviewed["calculated_at"].isoformat().replace(
        "+00:00", "Z"
    )
    await process_correction(session, job["id"], "reviewer")
    await process_correction(session, job["id"], "reviewer")
    assert await session.scalar(select(func.count()).select_from(UsageCostAdjustment)) == 2


@pytest.mark.asyncio
async def test_preview_cannot_be_reused_for_changed_rates_or_actor(session):
    model = await ensure_model(session, "gpt-4o", seed=True)
    await session.commit()
    at = datetime.now(UTC) - timedelta(days=2)
    body = correction_body(model, at)
    body.preview_token = (await preview(model.id, body, ADMIN, session))["preview_token"]
    with pytest.raises(HTTPException) as changed:
        await correct(model.id, body.model_copy(update={"reason": "Changed"}), ADMIN, session)
    assert changed.value.status_code == 409
    with pytest.raises(HTTPException) as actor:
        await correct(model.id, body, ADMIN.model_copy(update={"consumer_id": "other"}), session)
    assert actor.value.status_code == 409
    with pytest.raises(HTTPException) as tampered:
        assert body.preview_token is not None
        await correct(
            model.id,
            body.model_copy(update={"preview_token": body.preview_token + "x"}),
            ADMIN,
            session,
        )
    assert tampered.value.status_code == 409


@pytest.mark.asyncio
async def test_locked_catalog_refreshes_cached_generation(session):
    model = await ensure_model(session, "gpt-4o", seed=True)
    await session.commit()
    cached_generation = model.active_pricing_generation_id
    # Simulate another writer changing the row, without synchronizing ORM state.
    await session.execute(
        update(cast(Table, ModelCatalogEntry.__table__))
        .where(ModelCatalogEntry.id == model.id)
        .values(active_pricing_generation_id=None)
    )
    await session.commit()
    assert model.active_pricing_generation_id == cached_generation
    values = event_values(datetime.now(UTC))
    await price_event(session, values)
    assert values["cost_usd"] is None


@pytest.mark.asyncio
async def test_simultaneous_legacy_backfills_do_not_duplicate_adjustments(session, monkeypatch):
    if session.get_bind().dialect.name != "postgresql":
        pytest.skip("Row lock ordering requires PostgreSQL")
    model = await ensure_model(session, "gpt-4o", seed=True)
    await session.commit()
    at = datetime.now(UTC) - timedelta(days=2)
    body = correction_body(model, at)
    body.preview_token = (await preview(model.id, body, ADMIN, session))["preview_token"]
    job = await correct(model.id, body, ADMIN, session)
    await process_correction(session, job["id"], "reviewer")
    session.add(AgentEvent(**(event_values(at) | {"cost_usd": Decimal("2.5")})))
    await session.commit()
    import apex.services.model_pricing as pricing

    original = pricing.ensure_model
    arrivals = 0
    both_ready = asyncio.Event()

    async def synchronized_ensure(*args, **kwargs):
        nonlocal arrivals
        arrivals += 1
        if arrivals == 2:
            both_ready.set()
        await asyncio.wait_for(both_ready.wait(), 5)
        return await original(*args, **kwargs)

    monkeypatch.setattr(pricing, "ensure_model", synchronized_ensure)
    factory = async_sessionmaker(session.bind, expire_on_commit=False)
    async with factory() as first, factory() as second:
        await asyncio.gather(reconcile_models(first, []), reconcile_models(second, []))
    assert await session.scalar(select(func.count()).select_from(UsageCostAdjustment)) == 1


@pytest.mark.asyncio
async def test_registry_availability_and_default_guard_use_exact_provider_identity(session):
    from apex.routers.model_pricing import ModelWrite, archive_change, list_models, model_response
    from tests.unit.test_model_pricing_history import SETTINGS

    configured = await ensure_model(session, "gpt-4o", seed=True)
    historical = await ensure_model(session, "gpt-4o-mini", "unknown", seed=True)
    await session.commit()
    assert (await model_response(session, historical, SETTINGS))[
        "availability"
    ] == "historical_only"
    result = await list_models(
        ADMIN,
        session,
        cast(ApexSettings, SETTINGS),
        status="active",
        q="",
        provider=None,
        missing_pricing=False,
        limit=20,
        offset=0,
    )
    assert [row["id"] for row in result["items"]] == [configured.id]
    archived = await archive_change(
        historical.id,
        ModelWrite(
            expected_version=historical.version,
            reason="Retire historical identity",
            idempotency_key="archive-unknown",
        ),
        ADMIN,
        session,
        SETTINGS,
        True,
    )
    assert archived["availability"] == "archived"


@pytest.mark.parametrize("field", ["reason", "idempotency_key"])
def test_write_text_rejects_postgres_nul(field):
    from pydantic import ValidationError

    from apex.routers.model_pricing import ModelWrite

    payload = {"expected_version": 1, "reason": "Audit note", "idempotency_key": "once"}
    payload[field] = "invalid\x00text"
    with pytest.raises(ValidationError):
        ModelWrite.model_validate(payload)


@pytest.mark.parametrize(
    "url",
    [
        "/v1/analytics/models?q=bad%00query",
        "/v1/analytics/models?provider=bad%00query",
        "/v1/analytics/models/bad%00id",
        "/v1/analytics/models/model/corrections/bad%00id",
    ],
)
def test_model_admin_query_and_path_reject_postgres_nul(url, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from apex.app.dependencies import get_current_identity
    from apex.persistence.db import get_session
    from apex.routers.model_pricing import router

    async def unused_session():
        yield None

    app = FastAPI()
    app.include_router(router, prefix="/v1")
    app.dependency_overrides[get_current_identity] = lambda: ADMIN
    app.dependency_overrides[get_session] = unused_session
    with TestClient(app) as client:
        assert client.get(url).status_code == 422


@pytest.mark.asyncio
async def test_revision_replay_keys_preserve_original_attribution_time_and_cost(
    session, monkeypatch
):
    from apex.services import usage

    model = await ensure_model(session, "gpt-4o", seed=True)
    await session.commit()
    occurred_at = datetime.now(UTC) - timedelta(days=2)
    engine = session.bind
    assert engine is not None
    monkeypatch.setattr(usage, "create_async_engine", lambda *args, **kwargs: engine)

    async def retain_test_engine(_engine):
        pass

    monkeypatch.setattr(usage, "dispose_engine_instance_definitively", retain_test_engine)
    arguments: dict[str, Any] = dict(
        thread_id="revision-replay",
        project_id="p",
        app_id="a",
        phase="reporting",
        agent_name="reporting.worker",
        model="gpt-4o",
        provider="openai",
        attempt=1,
        status="ok",
        latency_ms=12,
        usage={"input_tokens": 1_000_000, "output_tokens": 0, "total_tokens": 1_000_000},
    )
    await usage.record_agent_event(**arguments, consumer_id="alice", occurred_at=occurred_at)
    await usage.record_agent_event(
        **arguments, revision=1, consumer_id="bob", occurred_at=occurred_at + timedelta(minutes=1)
    )
    # Explicit first-revision replay must match the pre-revision event-key contract.
    # Neither a new actor nor a new occurrence time/token count may rewrite evidence.
    for revision in (0, 1):
        await usage.record_agent_event(
            **(arguments | {"usage": {"input_tokens": 9_000_000}, "latency_ms": 999}),
            revision=revision,
            consumer_id="changed-owner",
            occurred_at=occurred_at + timedelta(days=1),
        )
    rows = (await session.scalars(select(AgentEvent).order_by(AgentEvent.at))).all()
    assert len(rows) == 2
    baseline_key = usage._replay_event_key(
        "agent",
        thread_id="revision-replay",
        phase="reporting",
        attempt=1,
        agent_name="reporting.worker",
        project_id="p",
        app_id="a",
    )
    assert rows[0].event_key == baseline_key
    assert rows[1].event_key != baseline_key
    assert [row.consumer_id for row in rows] == ["alice", "bob"]
    from apex.services.model_pricing import utc

    assert [utc(row.at) for row in rows] == [occurred_at, occurred_at + timedelta(minutes=1)]
    assert all(row.input_tokens == 1_000_000 and row.latency_ms == 12 for row in rows)
    assert all(row.cost_usd == Decimal("2.500000") for row in rows)
    assert all(row.model_catalog_id == model.id for row in rows)
