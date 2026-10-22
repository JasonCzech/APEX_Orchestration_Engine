"""Database-backed pricing invariants, retained corrections and user isolation."""

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.persistence.models import (
    AgentEvent,
    Base,
    ModelCatalogEntry,
    PricingCorrectionJob,
    UsageCostAdjustment,
)
from apex.routers.model_pricing import (
    CorrectionWrite,
    ModelWrite,
    PriceWrite,
    Rates,
    archive_change,
    correct,
    preview,
    set_price,
)
from apex.routers.usage_history import csv_cell
from apex.services.analytics_scope import AnalyticsVisibility
from apex.services.model_pricing import (
    cost_with_rates,
    effective_cost,
    ensure_model,
    intervals,
    price_event,
    process_correction,
)

ADMIN = ConsumerIdentity(
    consumer_id="admin", name="Admin", consumer_type=ConsumerType.DASHBOARD, role=Role.ADMIN
)
SETTINGS = SimpleNamespace(
    llm=SimpleNamespace(allowed_models=["gpt-4o", "gpt-4o-mini"], default_model="gpt-4o-mini")
)
RATES = Rates(input="4", output="8", cache_read="0.5", cache_creation="5")


@pytest_asyncio.fixture(
    params=["sqlite"] + (["postgres"] if os.environ.get("APEX_PRICING_TEST_URI") else [])
)
async def session(monkeypatch, request):
    monkeypatch.setenv("APEX_MODEL_PRICING_ADMIN_ENABLED", "true")
    from apex.settings import get_settings

    get_settings.cache_clear()
    schema = "pricing_test_" + uuid4().hex if request.param == "postgres" else None
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


def event_values(at, *, user="alice"):
    return dict(
        at=at,
        model="gpt-4o",
        provider="openai",
        consumer_id=user,
        project_id="p",
        app_id="a",
        phase="execution",
        agent_name="worker",
        status="ok",
        input_tokens=1_000_000,
        output_tokens=0,
        total_tokens=1_000_000,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        reasoning_tokens=0,
        extra={},
    )


@pytest.mark.asyncio
async def test_seed_alias_exact_rates_and_idempotent_reconcile(session):
    row = await ensure_model(session, "claude-sonnet-4-5", seed=True)
    timeline = await intervals(session, row.active_pricing_generation_id)
    assert timeline[0].input == Decimal("3")
    original = row.active_pricing_generation_id
    await ensure_model(session, "claude-sonnet-4-5", seed=True)
    assert row.active_pricing_generation_id == original
    unknown = await ensure_model(session, "claude-future-unknown", seed=True)
    assert unknown.active_pricing_generation_id is None


@pytest.mark.asyncio
async def test_correction_atomic_original_retained_late_events_and_idempotency(session):
    model = await ensure_model(session, "gpt-4o", seed=True)
    at = datetime.now(UTC) - timedelta(days=2)
    values = event_values(at)
    await price_event(session, values)
    event = AgentEvent(**values)
    session.add(event)
    await session.commit()
    assert event.cost_usd == Decimal("2.500000")
    body = CorrectionWrite(
        expected_version=model.version,
        reason="Correct contract rate",
        idempotency_key="correction-1",
        rates=RATES,
        effective_from=at - timedelta(hours=1),
        effective_to=at + timedelta(hours=1),
    )
    review = await preview(model.id, body, ADMIN, session)
    assert review["after_cost_usd"] == "4.000000"
    body.preview_token = review["preview_token"]
    result = await correct(model.id, body, ADMIN, session)
    repeated = await correct(model.id, body, ADMIN, session)
    assert repeated["id"] == result["id"]
    job_id = result["id"]
    await session.rollback()
    await process_correction(session, job_id, "worker")
    assert await session.scalar(select(effective_cost()).select_from(AgentEvent)) == Decimal(
        "2.500000"
    )
    await session.commit()
    await process_correction(session, job_id, "worker")
    assert await session.scalar(select(effective_cost()).select_from(AgentEvent)) == Decimal(
        "4.000000"
    )
    await session.refresh(event)
    assert event.cost_usd == Decimal("2.500000")
    assert (await session.get(PricingCorrectionJob, job_id)).state == "succeeded"
    late = event_values(at, user="bob")
    await price_event(session, late)
    assert late["cost_usd"] == Decimal("4.000000")
    assert len((await session.scalars(select(UsageCostAdjustment))).all()) == 1


@pytest.mark.asyncio
async def test_archive_retains_history_protects_default_and_checks_version(session):
    model = await ensure_model(session, "gpt-4o", seed=True)
    await session.commit()
    generation = model.active_pricing_generation_id
    body = ModelWrite(
        expected_version=model.version, reason="Retiring model", idempotency_key="archive-1"
    )
    result = await archive_change(model.id, body, ADMIN, session, SETTINGS, True)
    assert result["availability"] == "archived"
    assert model.active_pricing_generation_id == generation
    repeated = await archive_change(model.id, body, ADMIN, session, SETTINGS, True)
    assert repeated["version"] == result["version"]
    with pytest.raises(HTTPException) as stale:
        await archive_change(
            model.id,
            body.model_copy(update={"idempotency_key": "different"}),
            ADMIN,
            session,
            SETTINGS,
            False,
        )
    assert stale.value.status_code == 409
    default = await ensure_model(session, "gpt-4o-mini", seed=True)
    with pytest.raises(HTTPException):
        await archive_change(
            default.id,
            body.model_copy(update={"expected_version": default.version}),
            ADMIN,
            session,
            SETTINGS,
            True,
        )


@pytest.mark.asyncio
async def test_users_same_application_isolated_and_unknown_admin_only(session):
    for user in ["alice", "bob", None]:
        session.add(AgentEvent(**event_values(datetime.now(UTC), user=user)))
    session.add(
        AgentEvent(**(event_values(datetime.now(UTC), user="alice") | {"project_id": "outside"}))
    )
    await session.commit()
    alice = ConsumerIdentity(
        consumer_id="alice",
        name="Alice",
        consumer_type=ConsumerType.DASHBOARD,
        role=Role.VIEWER,
        scopes=[ScopeRef(project_id="p", app_id="a")],
    )
    visible = AnalyticsVisibility.resolve(alice)
    events = (await session.scalars(select(AgentEvent).where(*visible.filters(AgentEvent)))).all()
    assert len(events) == 1 and events[0].consumer_id == "alice"
    with pytest.raises(HTTPException):
        AnalyticsVisibility.resolve(alice, consumer_id="bob")
    with pytest.raises(HTTPException):
        AnalyticsVisibility.resolve(alice, view="authorized")
    scoped_admin = alice.model_copy(update={"role": Role.ADMIN})
    assert (
        len(
            (
                await session.scalars(
                    select(AgentEvent).where(
                        *AnalyticsVisibility.resolve(scoped_admin).filters(AgentEvent)
                    )
                )
            ).all()
        )
        == 3
    )


@pytest.mark.asyncio
async def test_zero_price_and_interval_boundaries(session):
    model = await ensure_model(session, "gpt-4o", seed=True)
    await session.commit()
    start = datetime.now(UTC) + timedelta(days=1)
    body = PriceWrite(
        expected_version=model.version,
        reason="Free promotion",
        idempotency_key="free",
        rates=Rates(input="0", output="0", cache_read="0", cache_creation="0"),
        effective_from=start,
    )
    await set_price(model.id, body, ADMIN, session, SETTINGS)
    before, after = event_values(start - timedelta(microseconds=1)), event_values(start)
    await price_event(session, before)
    await price_event(session, after)
    assert before["cost_usd"] == Decimal("2.500000")
    assert after["cost_usd"] == Decimal("0.000000")


def test_cache_breakdown_and_csv_formula_safety():
    usage = event_values(datetime.now(UTC)) | {
        "cache_read_tokens": 500_000,
        "cache_creation_tokens": 250_000,
    }
    assert cost_with_rates(usage, RATES.model_dump()) == Decimal("2.500000")
    assert csv_cell(' =HYPERLINK("x")').startswith("'")
    assert csv_cell("normal") == "normal"


@pytest.mark.asyncio
async def test_admission_archive_and_resume(session, monkeypatch):
    from contextlib import asynccontextmanager

    from apex.services.model_pricing import admit_models
    from apex.settings import get_settings

    monkeypatch.setenv("APEX_LLM__DEFAULT_MODEL", "gpt-4o-mini")
    monkeypatch.setenv("APEX_LLM__ALLOWED_MODELS", '["gpt-4o", "gpt-4o-mini"]')
    get_settings.cache_clear()

    @asynccontextmanager
    async def use_session():
        yield session

    monkeypatch.setattr("apex.persistence.db.get_sessionmaker", lambda: use_session)
    selected = await admit_models(
        {"model_by_phase": {"execution": "gpt-4o"}}, thread_id="thread", actor_id="alice"
    )
    # A later admission may be rejected by native multitask policy. It must not
    # replace the original execution's retained selection.
    await admit_models({}, thread_id="thread", actor_id="alice")
    model = await session.scalar(
        select(ModelCatalogEntry).where(ModelCatalogEntry.model_key == "gpt-4o")
    )
    await archive_change(
        model.id,
        ModelWrite(expected_version=model.version, reason="Archive", idempotency_key="archive"),
        ADMIN,
        session,
        SETTINGS,
        True,
    )
    assert (
        await admit_models(
            {"model_by_phase": selected}, thread_id="thread", actor_id="alice", resume=True
        )
        == selected
    )
    with pytest.raises(HTTPException):
        await admit_models(
            {"model_by_phase": {"execution": "gpt-4o"}}, thread_id="new-thread", actor_id="alice"
        )
    with pytest.raises(HTTPException):
        await admit_models(
            {"model_by_phase": {"execution": "gpt-4o"}}, thread_id="thread", actor_id="alice"
        )


@pytest.mark.asyncio
async def test_http_history_export_and_detail_cannot_disclose_other_users(session, monkeypatch):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from apex.app.dependencies import get_current_identity
    from apex.persistence.db import get_session
    from apex.routers.model_pricing import router as pricing_router
    from apex.routers.usage_history import router
    from apex.settings import get_settings

    monkeypatch.setenv("APEX_ANALYTICS_COST_VISIBLE", "true")
    get_settings.cache_clear()
    alice = ConsumerIdentity(
        consumer_id="alice",
        name="Alice",
        consumer_type=ConsumerType.DASHBOARD,
        role=Role.VIEWER,
        scopes=[ScopeRef(project_id="p", app_id="a")],
    )
    owned, other = (
        AgentEvent(**event_values(datetime.now(UTC))),
        AgentEvent(**event_values(datetime.now(UTC), user="bob")),
    )
    session.add_all([owned, other])
    await session.commit()
    app = FastAPI()
    app.include_router(router, prefix="/v1")
    app.include_router(pricing_router, prefix="/v1")

    async def current_session():
        yield session

    app.dependency_overrides[get_session] = current_session
    app.dependency_overrides[get_current_identity] = lambda: alice
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/analytics/agents/events")
        assert response.status_code == 200
        assert [item["consumer_id"] for item in response.json()["items"]] == ["alice"]
        assert (await client.get(f"/v1/analytics/agents/events/{other.id}")).status_code == 404
        assert (await client.get(f"/v1/analytics/agents/events/{owned.id}")).status_code == 200
        assert (await client.get("/v1/analytics/agents/events?consumer_id=bob")).status_code == 403
        choices = await client.get("/v1/analytics/agents/facets")
        assert choices.status_code == 200
        assert choices.json()["users"] == []
        assert choices.json()["applications"][0]["app_id"] == "a"
        assert (await client.get("/v1/analytics/agents/facets?consumer_id=bob")).status_code == 403
        exported = await client.get("/v1/analytics/agents/export")
        assert exported.status_code == 200
        assert "alice" in exported.text and "bob" not in exported.text
        assert (await client.get("/v1/analytics/models")).status_code == 403


@pytest.mark.asyncio
async def test_correction_resume_catches_late_insert_and_hides_staged_rows(session):
    model = await ensure_model(session, "gpt-4o", seed=True)
    at = datetime.now(UTC) - timedelta(days=2)
    for _ in range(501):
        session.add(
            AgentEvent(
                **(event_values(at) | {"model_catalog_id": model.id, "cost_usd": Decimal("2.5")})
            )
        )
    await session.commit()
    body = CorrectionWrite(
        expected_version=model.version,
        reason="Bulk correction",
        idempotency_key="bulk",
        rates=RATES,
        effective_from=at - timedelta(hours=1),
        effective_to=at + timedelta(hours=1),
    )
    review = await preview(model.id, body, ADMIN, session)
    body.preview_token = review["preview_token"]
    job = await correct(model.id, body, ADMIN, session)
    await process_correction(session, job["id"], "first")
    assert (await session.get(PricingCorrectionJob, job["id"])).processed_count == 500
    values = event_values(at)
    await price_event(session, values)
    session.add(AgentEvent(**values))
    await session.commit()
    record = await session.get(PricingCorrectionJob, job["id"])
    record.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await session.commit()
    await process_correction(session, job["id"], "replacement")
    await session.refresh(record)
    assert record.processed_count == 502
    assert record.state == "running"
    await process_correction(session, job["id"], "replacement")
    await session.refresh(record)
    assert record.state == "succeeded"
    costs = (await session.scalars(select(effective_cost()).select_from(AgentEvent))).all()
    assert len(costs) == 502 and all(cost == Decimal("4") for cost in costs)


@pytest.mark.asyncio
async def test_postgres_aggregates_respect_private_visibility_and_corrected_cost(session):
    if session.get_bind().dialect.name != "postgresql":
        pytest.skip("PostgreSQL percentile/date_trunc semantics")
    from apex.services.agent_analytics import AgentAnalyticsRepository

    at = datetime.now(UTC) - timedelta(days=1)
    await ensure_model(session, "gpt-4o", seed=True)
    for who in ["alice", "bob"]:
        values = event_values(at, user=who)
        await price_event(session, values)
        session.add(AgentEvent(**values))
    await session.commit()
    result = await AgentAnalyticsRepository(session).aggregate(
        window_from=at - timedelta(hours=1),
        window_to=at + timedelta(hours=1),
        bucket="day",
        group_by="user",
        consumer_id="alice",
        visible_scopes=(ScopeRef(project_id="p", app_id="a"),),
    )
    assert result["totals"]["events"] == 1
    assert result["totals"]["cost_usd"] == 2.5
    assert result["totals"]["cost_complete"] is True
    assert result["breakdown"][0]["key"] == "alice"


@pytest.mark.parametrize("value", ["-1", "NaN", "Infinity", "1000000000", "0.0000000001"])
def test_rates_reject_invalid_or_overprecision_values(value):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Rates(input=value, output="1", cache_read="1", cache_creation="1")


@pytest.mark.asyncio
async def test_stale_correction_preview_and_changed_idempotency_payload(session):
    model = await ensure_model(session, "gpt-4o", seed=True)
    await session.commit()
    at = datetime.now(UTC) - timedelta(days=2)
    body = CorrectionWrite(
        expected_version=model.version,
        reason="History change",
        idempotency_key="stale",
        rates=RATES,
        effective_from=at,
        effective_to=at + timedelta(days=1),
    )
    review = await preview(model.id, body, ADMIN, session)
    body.preview_token = review["preview_token"]
    await archive_change(
        model.id,
        ModelWrite(expected_version=model.version, reason="Archive", idempotency_key="archive"),
        ADMIN,
        session,
        SETTINGS,
        True,
    )
    with pytest.raises(HTTPException) as error:
        await correct(model.id, body, ADMIN, session)
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_backfill_after_publication_keeps_original_and_applies_correction(session):
    from apex.services.model_pricing import reconcile_models

    model = await ensure_model(session, "gpt-4o", seed=True)
    await session.commit()
    at = datetime.now(UTC) - timedelta(days=2)
    body = CorrectionWrite(
        expected_version=model.version,
        reason="Backdated rates",
        idempotency_key="backfill",
        rates=RATES,
        effective_from=at,
        effective_to=at + timedelta(days=1),
    )
    review = await preview(model.id, body, ADMIN, session)
    body.preview_token = review["preview_token"]
    job = await correct(model.id, body, ADMIN, session)
    await process_correction(session, job["id"], "worker")
    legacy = AgentEvent(**(event_values(at + timedelta(hours=1)) | {"cost_usd": Decimal("2.5")}))
    session.add(legacy)
    await session.commit()
    await reconcile_models(session, ["gpt-4o"])
    await session.refresh(legacy)
    assert legacy.cost_usd == Decimal("2.5")
    assert await session.scalar(
        select(effective_cost()).select_from(AgentEvent).where(AgentEvent.id == legacy.id)
    ) == Decimal("4")
