"""Durable native RCA receipts survive all graph/remote submission crash windows."""

import asyncio
from contextlib import asynccontextmanager
from typing import cast

import pytest
from langchain_core.runnables import RunnableConfig
from sqlalchemy import Table, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from apex.domain.pipeline import utcnow_iso
from apex.graphs.pipeline import analysis_phase
from apex.graphs.pipeline.state import PipelineState, merge_phase_results
from apex.persistence.models import NativeAnalysisSubmission
from apex.services import native_analysis_submissions as service


@pytest.fixture
async def database(tmp_path, monkeypatch):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'receipts.db'}",
        poolclass=NullPool,
        execution_options={"schema_translate_map": {"apex": None}},
    )
    async with engine.begin() as connection:
        await connection.run_sync(cast(Table, NativeAnalysisSubmission.__table__).create)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def receipt_session():
        async with factory() as session:
            yield session

    monkeypatch.setattr(service, "_receipt_session", receipt_session)
    yield factory
    await engine.dispose()


def scope(**changes):
    return service.NativeAnalysisScope(
        **{
            "thread_id": "native-receipt-test",
            "phase_attempt": 1,
            "project_id": "project",
            "connection_id": "native-connection",
            "native_run_id": "native-run",
            "input_digest": "e" * 64,
            "idempotency_key": "a" * 64,
            **changes,
        }
    )


async def test_sql_claim_and_receipt_are_immutable_across_new_sessions(database):
    binding = scope()
    assert await service.claim_native_analysis(binding) is None
    with pytest.raises(service.NativeAnalysisSubmissionUncertain):
        await service.claim_native_analysis(binding)
    await service.record_native_analysis_receipt(binding, "native-job")
    assert await service.claim_native_analysis(binding) == "native-job"
    await service.record_native_analysis_receipt(binding, "native-job")
    with pytest.raises(ValueError, match="conflicts"):
        await service.record_native_analysis_receipt(binding, "different-job")
    for changed in (
        {"thread_id": "other-thread"},
        {"phase_attempt": 2},
        {"connection_id": "other-connection"},
        {"native_run_id": "other-run"},
        {"input_digest": "f" * 64},
        {"project_id": "other-project"},
    ):
        with pytest.raises(ValueError, match="different inputs"):
            await service.claim_native_analysis(scope(**changed))
    async with database() as session:
        receipt = await session.scalar(select(NativeAnalysisSubmission))
        assert receipt.job_id == "native-job"


async def test_only_one_concurrent_worker_can_claim_before_post(database):
    async def attempt():
        try:
            return await service.claim_native_analysis(scope())
        except service.NativeAnalysisSubmissionUncertain:
            return "already-claimed"

    results = await asyncio.gather(attempt(), attempt())
    assert results.count(None) == 1
    assert results.count("already-claimed") == 1


def graph_fixture():
    config = {
        "configurable": {
            "thread_id": "native-receipt-test",
            "engine": "apex_load",
            "phases": ["reporting"],
            "analysis": {
                "selections": [{"tool": "internal_rca", "connection_id": "native-connection"}]
            },
        }
    }
    state = {
        "phase_results": {
            "reporting": {"attempt": 1},
            "execution": {
                "test_summary": {"engine": "apex_load"},
                "engine_handle": {
                    "external_run_id": "native-run",
                    "connection_id": "native-connection",
                },
            },
        }
    }
    typed_state = cast(PipelineState, state)
    typed_config = cast(RunnableConfig, config)
    apply(typed_state, analysis_phase.prepare_analysis(typed_state, typed_config))
    state.get("phase_results", {})["reporting"]["analysis_ledger"][0]["started_at"] = utcnow_iso()
    return typed_state, typed_config


def apply(state, update):
    state["phase_results"] = merge_phase_results(
        state.get("phase_results", {}), update["phase_results"]
    )


@pytest.mark.parametrize("crash_window", ["before_post", "after_post", "after_receipt"])
async def test_restart_never_reposts_a_claimed_native_submission(
    database, monkeypatch, crash_window
):
    state, config = graph_fixture()
    post_count = 0
    adapter_entered = 0

    class Adapter:
        async def submit_native_analysis(self, _run_id, *, idempotency_key):
            nonlocal post_count, adapter_entered
            adapter_entered += 1
            if crash_window == "before_post":
                raise asyncio.CancelledError()
            post_count += 1
            return {"analysis_id": "native-job", "state": "queued"}

    class Resolver:
        async def resolve_with_connection_id(self, *_args, **_kwargs):
            return Adapter(), "native-connection"

        async def close(self):
            pass

    monkeypatch.setattr(analysis_phase, "_make_analysis_resolver", Resolver)
    if crash_window == "after_post":

        async def crash_record(_scope, _job_id):
            raise asyncio.CancelledError()

        monkeypatch.setattr(analysis_phase, "record_native_analysis_receipt", crash_record)
    if crash_window == "after_receipt":
        # Discard the node result, exactly as a crash before graph checkpointing does.
        await analysis_phase.analysis_step(state, config)
    else:
        with pytest.raises(asyncio.CancelledError):
            await analysis_phase.analysis_step(state, config)
    replay = await analysis_phase.analysis_step(state, config)
    apply(state, replay)
    assert adapter_entered == 1
    assert post_count == (0 if crash_window == "before_post" else 1)
    row = state.get("phase_results", {})["reporting"]["analysis_ledger"][0]
    if crash_window == "after_receipt":
        assert row["job_id"] == "native-job"
        assert row["status"] == "queued"
    else:
        assert row["job_id"] is None
        assert row["submission_uncertain"] and row["status"] == "failed"
        assert analysis_phase.route_analysis(state) == "analysis_recovery"


async def test_storage_unavailable_never_reaches_native_post(monkeypatch):
    state, config = graph_fixture()

    class Adapter:
        async def submit_native_analysis(self, *_args, **_kwargs):
            pytest.fail("Native POST must wait until its reservation commits")

    class Resolver:
        async def resolve_with_connection_id(self, *_args, **_kwargs):
            return Adapter(), "native-connection"

        async def close(self):
            pass

    async def offline(_scope):
        raise RuntimeError("Native analysis submission persistence is unavailable")

    monkeypatch.setattr(analysis_phase, "_make_analysis_resolver", Resolver)
    monkeypatch.setattr(analysis_phase, "claim_native_analysis", offline)
    apply(state, await analysis_phase.analysis_step(state, config))
    row = state.get("phase_results", {})["reporting"]["analysis_ledger"][0]
    assert row["status"] == "failed"
    assert "persistence is unavailable" in row["error"]
