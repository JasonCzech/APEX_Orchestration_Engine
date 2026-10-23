"""Facade launches and reruns admit models themselves.

The in-process SDK transport bypasses LangGraph auth, so the native run-create
hook's model admission never runs for /v1 launches. These tests pin the facade
path that does: allowlist/archive refusal, defaults pinned at admission, an
admission record per thread, and no admission for an adopted replay.
"""

from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import apex.routers.pipelines as pipelines
import apex.services.pipeline_read as pipeline_read
from apex.persistence.models import Base, ModelCatalogEntry, ModelRunAdmission
from apex.persistence.repositories.documents import DocumentsRepository
from apex.services.pipeline_read import (
    ModelAdmissionError,
    ModelAdmissionUnavailableError,
    PipelineReadService,
)
from tests.unit.test_pipelines_create_run import (
    AtomicClient,
    FakeCatalogRepo,
    FakeClient,
    FakeDocsRepo,
    _identity,
    _scope_request,
)


@pytest.fixture(autouse=True)
def _fake_projection_writer(monkeypatch):
    from unittest.mock import AsyncMock

    monkeypatch.setattr("apex.services.test_scope.persist_test_scope", AsyncMock())


class DeletingThreads:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def create(self, metadata: Any = None) -> dict[str, str]:
        return {"thread_id": "thr-1"}

    async def delete(self, thread_id: str) -> None:
        self.deleted.append(thread_id)


def _recording_admission(monkeypatch, *, result=None, error: Exception | None = None):
    calls: list[dict[str, Any]] = []

    async def admit(configurable, *, thread_id, actor_id, resume=False):
        calls.append(
            {
                "configurable": dict(configurable),
                "thread_id": thread_id,
                "actor_id": actor_id,
                **({"resume": True} if resume else {}),
            }
        )
        if error is not None:
            raise error
        return result if result is not None else {"execution": "pinned-model"}

    monkeypatch.setattr(pipeline_read, "admit_models", admit)
    return calls


async def test_launch_admits_and_pins_models_before_the_run_exists(monkeypatch) -> None:
    client = FakeClient()
    calls = _recording_admission(monkeypatch, result={"reporting": "gpt-4o-mini"})

    async def prepare(configurable: dict[str, Any], _input: dict[str, Any]) -> None:
        # Admission runs first and is visible to later preparation steps.
        assert configurable["model_by_phase"] == {"reporting": "gpt-4o-mini"}

    await PipelineReadService(client).start_run(
        title="Admitted",
        phases=["reporting"],
        principal_id="operator-1",
        prepare_configurable=prepare,
    )

    assert calls == [
        {
            "configurable": calls[0]["configurable"],
            "thread_id": "thr-1",
            "actor_id": "operator-1",
        }
    ]
    assert client.runs.args[3]["configurable"]["model_by_phase"] == {"reporting": "gpt-4o-mini"}


async def test_an_adopted_replay_is_not_admitted_again(monkeypatch) -> None:
    client = AtomicClient()
    calls = _recording_admission(monkeypatch)
    launch: dict[str, Any] = {
        "title": "Retried",
        "idempotency_key": "retry-1",
        "principal_id": "operator-1",
    }

    first = await PipelineReadService(client).start_run(**launch)
    second = await PipelineReadService(client).start_run(**launch)

    assert first == second
    assert len(calls) == 1


@pytest.mark.parametrize(
    "error,expected",
    [
        (
            HTTPException(409, "Model gpt-4o is archived; select an active model"),
            ModelAdmissionError,
        ),
        (ValueError("bad phase window"), ModelAdmissionError),
        (ConnectionError("catalog down"), ModelAdmissionUnavailableError),
    ],
)
async def test_a_refused_admission_removes_the_fresh_thread(monkeypatch, error, expected) -> None:
    client = FakeClient()
    client.threads = cast(Any, DeletingThreads())
    _recording_admission(monkeypatch, error=error)

    with pytest.raises(expected):
        await PipelineReadService(client).start_run(title="Refused", principal_id="operator-1")

    assert client.threads.deleted == ["thr-1"]
    assert client.runs.args == ()


def _route_request(service: PipelineReadService) -> Any:
    return SimpleNamespace(
        app=SimpleNamespace(
            dependency_overrides={pipelines.get_pipeline_read_service: lambda: service}
        )
    )


@pytest.mark.parametrize(
    "error,status,detail",
    [
        (
            HTTPException(409, "Model gpt-4o is archived; select an active model"),
            409,
            "Model gpt-4o is archived; select an active model",
        ),
        (ConnectionError("catalog down"), 503, "Model availability cannot be checked; retry later"),
    ],
)
async def test_the_launch_route_names_admission_refusals(monkeypatch, error, status, detail):
    _recording_admission(monkeypatch, error=error)
    client = FakeClient()
    with pytest.raises(HTTPException) as excinfo:
        await pipelines.create_pipeline_run(
            _scope_request(title="Wizard launch"),
            _identity(["proj-1"]),
            cast(DocumentsRepository, FakeDocsRepo({})),
            cast(Any, FakeCatalogRepo()),
            cast(Any, _route_request(PipelineReadService(client))),
        )
    assert (excinfo.value.status_code, excinfo.value.detail) == (status, detail)
    assert client.runs.args == ()


async def test_the_real_admission_refuses_an_archived_model_on_a_new_launch(monkeypatch) -> None:
    from apex.services.model_pricing import admit_models
    from apex.settings import get_settings

    monkeypatch.setenv("APEX_LLM__DEFAULT_MODEL", "gpt-4o-mini")
    monkeypatch.setenv("APEX_LLM__ALLOWED_MODELS", '["gpt-4o", "gpt-4o-mini"]')
    get_settings.cache_clear()
    engine = create_async_engine(
        "sqlite+aiosqlite://", execution_options={"schema_translate_map": {"apex": None}}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("apex.persistence.db.get_sessionmaker", lambda: factory)
    monkeypatch.setattr(pipeline_read, "admit_models", admit_models)
    try:
        client = FakeClient()
        await PipelineReadService(client).start_run(
            title="First",
            phases=["reporting"],
            model_by_phase={"reporting": "gpt-4o"},
            principal_id="operator-1",
        )
        pinned = client.runs.args[3]["configurable"]["model_by_phase"]
        # Defaults are pinned for every agent phase, explicit choices kept.
        assert pinned["reporting"] == "gpt-4o"
        assert pinned["story_analysis"] == "gpt-4o-mini"
        async with factory() as session:
            admission = await session.scalar(select(ModelRunAdmission))
            assert admission is not None
            assert (admission.thread_id, admission.actor_id) == ("thr-1", "operator-1")
            model = await session.scalar(
                select(ModelCatalogEntry).where(ModelCatalogEntry.model_key == "gpt-4o")
            )
            assert model is not None
            from datetime import UTC, datetime

            model.archived_at = datetime.now(UTC)
            await session.commit()

        refused = FakeClient()
        with pytest.raises(ModelAdmissionError, match="archived"):
            await PipelineReadService(refused).start_run(
                title="Second",
                phases=["reporting"],
                model_by_phase={"reporting": "gpt-4o"},
                principal_id="operator-1",
            )
        assert refused.runs.args == ()
    finally:
        await engine.dispose()
        get_settings.cache_clear()


async def test_a_sparse_launch_is_admitted_with_the_assistants_model_choices(monkeypatch):
    calls = _recording_admission(monkeypatch)

    class AssistantWithModels:
        async def get(self, assistant_id):
            return {
                "graph_id": "pipeline",
                "config": {"configurable": {"model_by_phase": {"reporting": "gpt-4o"}}},
            }

    client = FakeClient()
    client.assistants = cast(Any, AssistantWithModels())
    await PipelineReadService(client).start_run(title="Sparse", phases=["reporting"])

    assert calls[0]["configurable"]["model_by_phase"] == {"reporting": "gpt-4o"}
    # The run still carries only what the caller sent plus the pinned result.
    assert client.runs.args[3]["configurable"]["model_by_phase"] == {"execution": "pinned-model"}


async def test_a_bound_thread_retried_by_cycle_dispatch_keeps_its_admission(monkeypatch):
    """A retry on an already-bound thread must not be re-decided and stranded."""

    calls = _recording_admission(monkeypatch)
    client = AtomicClient()
    service = PipelineReadService(client)
    launch: dict[str, Any] = {
        "title": "Cycle execution",
        "idempotency_key": "cycle-dispatch:d-1",
        "principal_id": "author",
        "reuse_existing_thread": True,
    }
    # First attempt: the thread does not exist yet, so admission decides.
    await service.start_run(**launch)
    thread_id = next(iter(client.threads.rows))
    # Simulate the bound thread whose run was never submitted.
    client.runs.rows.clear()
    await service.start_run(**launch)

    assert [call.get("resume", False) for call in calls] == [False, True]
    assert {call["thread_id"] for call in calls} == {thread_id}
