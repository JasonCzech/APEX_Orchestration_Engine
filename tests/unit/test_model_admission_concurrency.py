"""PostgreSQL admission/archive ordering across independent transactions."""

import asyncio
import os
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from apex.auth.identity import ConsumerIdentity, ConsumerType, Role
from apex.persistence.models import Base
from apex.routers.model_pricing import ModelWrite, archive_change
from apex.services import model_pricing
from apex.settings import get_settings


@pytest.mark.asyncio
@pytest.mark.skipif(not os.getenv("APEX_PRICING_TEST_URI"), reason="PostgreSQL integration")
async def test_archive_commits_before_waiting_admission_is_rejected(monkeypatch):
    schema = "admission_test_" + uuid4().hex
    engine = create_async_engine(
        os.environ["APEX_PRICING_TEST_URI"],
        execution_options={"schema_translate_map": {"apex": schema}},
    )
    monkeypatch.setenv("APEX_MODEL_PRICING_ADMIN_ENABLED", "true")
    monkeypatch.setenv("APEX_LLM__DEFAULT_MODEL", "gpt-4o-mini")
    monkeypatch.setenv("APEX_LLM__ALLOWED_MODELS", '["gpt-4o", "gpt-4o-mini"]')
    get_settings.cache_clear()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("apex.persistence.db.get_sessionmaker", lambda: factory)
    admission_task = None
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            await conn.run_sync(Base.metadata.create_all)
        async with factory() as session:
            model = await model_pricing.ensure_model(session, "gpt-4o", seed=True)
            await session.commit()
            model_id = model.id

        started = asyncio.Event()
        original_ensure = model_pricing.ensure_model

        async def notify_ensure(*args, **kwargs):
            started.set()
            return await original_ensure(*args, **kwargs)

        monkeypatch.setattr(model_pricing, "ensure_model", notify_ensure)
        async with factory() as archive_session:
            model = await model_pricing.locked_model(archive_session, model_id)
            admission_task = asyncio.create_task(
                model_pricing.admit_models(
                    {"model_by_phase": {"execution": "gpt-4o"}},
                    thread_id="waiting-admission",
                    actor_id="alice",
                )
            )
            await asyncio.wait_for(started.wait(), timeout=5)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(admission_task), timeout=0.1)
            await archive_change(
                model_id,
                ModelWrite(
                    expected_version=model.version, reason="Retire", idempotency_key="retire"
                ),
                ConsumerIdentity(
                    consumer_id="admin",
                    name="Admin",
                    role=Role.ADMIN,
                    consumer_type=ConsumerType.DASHBOARD,
                ),
                archive_session,
                get_settings(),
                True,
            )
        with pytest.raises(HTTPException) as rejected:
            await asyncio.wait_for(admission_task, timeout=5)
        assert rejected.value.status_code == 409
        assert "archived" in rejected.value.detail
    finally:
        if admission_task is not None and not admission_task.done():
            admission_task.cancel()
            await asyncio.gather(admission_task, return_exceptions=True)
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()
        get_settings.cache_clear()
