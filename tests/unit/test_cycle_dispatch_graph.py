"""Dispatch HTTP reaches the real facade, graph, adapter and durable result projection.

Only external boundaries are substituted: loopback transport invokes the compiled
in-memory graph, the provider speaks through respx, and SQLite replaces Postgres.
The preparation eligibility fixture is separately covered by dispatch-context tests.
"""

import hashlib
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import DateTime, create_engine, event, inspect, select, update
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from apex.adapters.registry import ConnectionConfig, PortKind
from apex.adapters.stubs import MemoryArtifactStore
from apex.app.dependencies import get_current_identity
from apex.auth.handlers import on_threads_create, on_threads_create_run
from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.domain.cycle_executions import ExecutionSnapshot, PreflightCheck
from apex.graphs.pipeline import execution_phase, phase_subgraph
from apex.graphs.pipeline.graph import builder
from apex.persistence import db
from apex.persistence.db import get_session
from apex.persistence.models import (
    ApiConsumer,
    Application,
    Base,
    Connection,
    ConsumerScope,
    CycleExecutionDispatch,
    CycleExecutionPreparation,
    EngineRun,
    Environment,
)
from apex.persistence.models import TestCycle as CycleRow
from apex.routers import cycle_dispatch as api
from apex.routers import cycle_executions, cycle_scripts
from apex.routers.cycles import enabled, find
from apex.routers.pipelines import get_pipeline_read_service
from apex.services import artifact_references, cycle_dispatch, engine_runs
from apex.services.connections import ResolvedAdapter
from apex.services.pipeline_read import PipelineReadService
from tests.unit.test_apex_load_approved_source import BASE, SOURCE, Provider, adapter
from tests.unit.test_auth_handlers import make_ctx
from tests.unit.test_cycle_dispatch import AsyncFacade
from tests.unit.test_pipelines_facade import _not_found


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario", ["success", "changed_approval", "revoked_actor", "target_removed", "app_archived"]
)
async def test_dispatch_http_runs_exact_approved_source_and_links_persisted_results(
    tmp_path, monkeypatch, scenario
):
    schema_path = str(tmp_path / "apex.sqlite")
    db_path = str(tmp_path / "main.sqlite")

    def attach(connection, _record):
        connection.execute("ATTACH DATABASE ? AS apex", (schema_path,))

    database = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    event.listen(database, "connect", attach)
    Base.metadata.create_all(database)

    # SQLite drops DateTime(timezone=True) offsets; restore the PostgreSQL UTC
    # representation at the database adapter boundary, without changing rows.
    def restore_timezone(_session, instance):
        for attr in inspect(instance).mapper.column_attrs:
            value = getattr(instance, attr.key)
            if (
                isinstance(attr.columns[0].type, DateTime)
                and isinstance(value, datetime)
                and value.tzinfo is None
            ):
                set_committed_value(instance, attr.key, value.replace(tzinfo=UTC))

    event.listen(Session, "loaded_as_persistent", restore_timezone)

    class DatabaseFacade(AsyncFacade):
        def get_bind(self):
            return self.session.get_bind()

        async def get(self, model, key):
            return self.session.get(model, key)

        async def delete(self, row):
            self.session.delete(row)

        async def flush(self):
            self.session.flush()

    @asynccontextmanager
    async def binding_session():
        with Session(database, expire_on_commit=False) as session:
            yield DatabaseFacade(session)

    class DisposableEngine:
        async def dispose(self):
            pass

    for module in (engine_runs, artifact_references):
        monkeypatch.setattr(
            module, "create_async_engine", lambda *_args, **_kwargs: DisposableEngine()
        )
        monkeypatch.setattr(module, "async_sessionmaker", lambda *_args, **_kwargs: binding_session)
    monkeypatch.setattr(cycle_dispatch, "get_sessionmaker", lambda: binding_session)
    monkeypatch.setattr(db, "get_sessionmaker", lambda: binding_session)
    actor = ConsumerIdentity(
        consumer_id="operator",
        name="Operator",
        consumer_type=ConsumerType.DASHBOARD,
        role=Role.OPERATOR,
        scopes=[ScopeRef(project_id="project", app_id="app")],
    )
    current_actor = actor

    version = datetime(2026, 10, 17, 12, tzinfo=UTC)
    source = json.dumps(SOURCE)
    checksum = hashlib.sha256(source.encode()).hexdigest()
    snapshot = ExecutionSnapshot(
        plan_version_id="plan",
        plan_title="Approved plan",
        plan_checksum="plan-sha",
        script_version_id="script",
        script_title="Approved exact source",
        script_checksum="script-sha",
        script_source_checksum=checksum,
        script_format="apex_native_json",
        readiness_version_id="readiness",
        readiness_checksum="ready-sha",
        engine="apex-load",
        mode="live",
        environment_id="env",
        environment_version=2,
        connection_id="connection",
        connection_version=version.isoformat(),
        launch_authority="pipeline",
        workload={"vusers": 1, "ramp_s": 0, "duration_s": 5, "slas": {"p95_ms": 1000}},
    )
    with Session(database) as session:
        session.add(Application(id="app", project_ids=["project"], name="Checkout"))
        session.add(
            Environment(
                id="env",
                application_id="app",
                name="Target",
                base_url="https://target.example",
                target_approved=True,
                target_version=2,
            )
        )
        session.add(
            ApiConsumer(
                id="operator",
                name="Operator",
                key_hash="a" * 64,
                consumer_type="dashboard",
                role="operator",
            )
        )
        session.add(ConsumerScope(consumer_id="operator", project_id="project", app_id="app"))
        session.add(
            CycleRow(
                id="cycle",
                project_id="project",
                app_id="app",
                environment_id="env",
                name="Approved cycle",
                mode="live",
                owner="operator",
                revision=1,
                independent_review=False,
                versions=[],
            )
        )
        session.add(
            CycleExecutionPreparation(
                id="prep",
                cycle_id="cycle",
                idempotency_key="prep",
                request_fingerprint="digest",
                snapshot=snapshot.model_dump(),
                created_by="operator",
            )
        )
        session.add_all(
            [
                Connection(
                    id="connection",
                    kind="execution_engine",
                    provider="apex_load",
                    project_id="project",
                    name="Load",
                    options={"native_control_enabled": True, "project_id": "project"},
                    runtime_version=version,
                ),
                Connection(
                    id="store",
                    kind="artifact_store",
                    provider="memory",
                    project_id="project",
                    name="Artifacts",
                    options={},
                    runtime_version=version,
                ),
            ]
        )
        session.commit()
    store_config = ConnectionConfig(
        id="store", kind=PortKind.ARTIFACT_STORE, provider="memory", name="Artifacts", options={}
    )
    MemoryArtifactStore.clear()
    store = MemoryArtifactStore(store_config)
    await store.put("approved/source.json", source.encode(), content_type="application/json")

    async def resolve_store(*_args, **_kwargs):
        return MemoryArtifactStore(store_config)

    monkeypatch.setattr(cycle_scripts, "resolve_store", resolve_store)

    checks = []

    async def context(session, identity, cycle_id, preparation_id):
        assert preparation_id == "prep"
        cycle = await find(session, identity, cycle_id)
        return (
            cycle,
            snapshot,
            SimpleNamespace(connection_id="store", artifact_key="approved/source.json"),
            SimpleNamespace(base_url="https://target.example", version=2),
            checks,
        )

    monkeypatch.setattr(api, "dispatch_context", context)
    monkeypatch.setattr(cycle_dispatch, "dispatch_context", context)

    class Resolver:
        async def resolve_with_metadata(self, kind, **kwargs):
            expected = "connection" if kind == PortKind.EXECUTION_ENGINE else "store"
            assert kwargs.get("connection_id") == expected
            return ResolvedAdapter(
                adapter=adapter()
                if kind == PortKind.EXECUTION_ENGINE
                else MemoryArtifactStore(store_config),
                connection_id=expected,
                connection_version=version,
                persisted=True,
            )

        async def resolve_with_connection_id(self, kind, **kwargs):
            resolved = await self.resolve_with_metadata(kind, **kwargs)
            return resolved.adapter, resolved.connection_id

        async def close(self):
            pass

    monkeypatch.setattr(execution_phase, "_make_resolver", Resolver)
    monkeypatch.setattr(phase_subgraph, "_make_artifact_resolver", Resolver)
    provider = Provider()
    provider.mount()
    original_read = provider.read

    def read(request):
        if provider.start_count:
            provider.test["status"] = "COMPLETE"
        return original_read(request)

    respx.get(BASE + "/api/v1/tests/test-1").mock(side_effect=read)
    respx.get(BASE + "/api/v1/tests/test-1/sla-status").respond(
        200,
        json={
            "test_id": "test-1",
            "status": "COMPLETE",
            "sla_breached": False,
            "sla_evaluated": True,
            "details": [],
        },
    )
    respx.get(url__regex=BASE + r"/api/v1/tests/test-1/archive/.*").respond(
        404, json={"detail": "No archive"}
    )
    respx.get(BASE + "/api/v1/tests/test-1/archive/report").respond(
        200, json={"manifest": {"test_id": "test-1"}, "summary": {}}
    )
    graph = builder.compile(checkpointer=InMemorySaver())

    class Loopback:
        def __init__(self):
            self.threads = SimpleNamespace(
                create=self.create_thread, get=self.get_thread, get_state=self.get_state
            )
            self.runs = SimpleNamespace(create=self.create_run, list=self.list_runs)
            self.thread_records = {}
            self.run_records = []
            self.result: dict[str, Any] = {}
            self.create_calls = 0
            self.thread_creates = 0

        async def create_thread(self, *, metadata, thread_id, **_kwargs):
            self.thread_creates += 1
            native: Any = {"metadata": metadata, "thread_id": thread_id}
            await on_threads_create(
                ctx=make_ctx(current_actor, action="create", trusted_loopback=True), value=native
            )
            metadata = native["metadata"]
            self.thread_records.setdefault(
                thread_id, {"thread_id": thread_id, "metadata": metadata}
            )
            return self.thread_records[thread_id]

        async def get_thread(self, thread_id):
            if thread_id not in self.thread_records:
                raise _not_found()
            return {**self.thread_records[thread_id], "status": "idle" if self.result else "busy"}

        async def get_state(self, thread_id, **_kwargs):
            return {
                "values": self.result,
                "next": [],
                "tasks": [],
                "checkpoint": {"thread_id": thread_id},
                "created_at": "2026-10-17T12:00:00Z",
            }

        async def list_runs(self, thread_id, **_kwargs):
            return self.run_records

        async def create_run(self, thread_id, assistant_id, *, input, config, metadata, **_kwargs):
            assert assistant_id == "pipeline"
            assert config["configurable"]["phases"] == ["execution"]
            assert "load_test" not in config["configurable"]
            self.create_calls += 1
            native: Any = {
                "assistant_id": assistant_id,
                "thread_id": thread_id,
                "metadata": metadata,
                "kwargs": {"input": input, "config": config},
            }
            await on_threads_create_run(
                ctx=make_ctx(current_actor, action="create_run", trusted_loopback=True),
                value=native,
            )
            config = native["kwargs"]["config"]
            if scenario != "success" and self.create_calls == 1:
                raise TimeoutError(
                    "Native root creation response lost after durable thread binding"
                )
            # Same graph configuration transport LangGraph supplies; no source or plan injection.
            config = {**config, "configurable": {**config["configurable"], "thread_id": thread_id}}
            self.result = await graph.ainvoke(input, cast(RunnableConfig, config))
            self.run_records.append({"run_id": "native-run", "metadata": metadata})
            return {"run_id": "native-run"}

    loopback = Loopback()
    pipeline = PipelineReadService(loopback)
    application = FastAPI()
    application.include_router(api.router, prefix="/v1")
    application.include_router(cycle_executions.router, prefix="/v1")
    application.dependency_overrides[enabled] = lambda: None
    application.dependency_overrides[get_current_identity] = lambda: current_actor
    application.dependency_overrides[get_pipeline_read_service] = lambda: pipeline

    def sessions():
        with Session(database, expire_on_commit=False) as session:
            yield AsyncFacade(session)

    application.dependency_overrides[get_session] = sessions
    try:
        with TestClient(application) as client:
            path = "/v1/cycles/cycle/executions/prep/dispatch"
            response = client.post(path, json={"expected_revision": 1, "idempotency_key": "launch"})
            assert response.status_code == 200, response.text
            receipt = response.json()["attempt"]
            if scenario != "success":
                assert receipt["status"] == "uncertain" and receipt["thread_id"]
                assert provider.start_count == provider.posts == provider.uploads == 0
                assert loopback.run_records == []
                if scenario == "changed_approval":
                    checks.append(
                        PreflightCheck(
                            key="approval",
                            label="Approval",
                            status="blocked",
                            detail="The approved version was revised",
                        )
                    )
                else:
                    with Session(database) as session:
                        if scenario == "revoked_actor":
                            session.execute(
                                update(ApiConsumer)
                                .where(ApiConsumer.id == "operator")
                                .values(enabled=False)
                            )
                            session.add(
                                ApiConsumer(
                                    id="recovering-operator",
                                    name="Recovering operator",
                                    key_hash="b" * 64,
                                    consumer_type="dashboard",
                                    role="operator",
                                )
                            )
                            session.add(
                                ConsumerScope(
                                    consumer_id="recovering-operator",
                                    project_id="project",
                                    app_id="app",
                                )
                            )
                            current_actor = ConsumerIdentity(
                                consumer_id="recovering-operator",
                                name="Recovering operator",
                                consumer_type=ConsumerType.DASHBOARD,
                                role=Role.OPERATOR,
                                scopes=[ScopeRef(project_id="project", app_id="app")],
                            )
                        elif scenario == "app_archived":
                            session.execute(
                                update(Application)
                                .where(Application.id == "app")
                                .values(archived_at=datetime.now(UTC))
                            )
                        else:
                            session.execute(
                                update(Environment)
                                .where(Environment.id == "env")
                                .values(target_approved=False, target_version=3)
                            )
                        session.commit()
                resumed = client.post(
                    path, json={"expected_revision": 2, "idempotency_key": "launch"}
                )
                assert resumed.status_code == 200, resumed.text
                assert resumed.json()["attempt"]["thread_id"] == receipt["thread_id"]
                assert loopback.result["phase_results"]["execution"]["status"] == "failed", (
                    loopback.result
                )
                assert loopback.create_calls == 2
                assert loopback.thread_creates == 1
                assert provider.start_count == provider.posts == provider.uploads == 0
                assert not respx.calls
                final = client.get(path).json()["attempt"]
                assert final["status"] == "failed", final
                with Session(database) as session:
                    row = session.scalar(select(CycleExecutionDispatch))
                    assert row is not None and row.active_environment_id is None
                return

            assert (
                loopback.result.get("phase_results", {}).get("execution", {}).get("status")
                == "succeeded"
            ), json.dumps(loopback.result, default=str, indent=2)
            assert provider.start_count == provider.posts == provider.uploads == 1
            assert provider.script["actions"] == SOURCE["actions"]
            assert provider.script["config"] == SOURCE["config"]
            assert provider.test["duration"] == "5s"
            with Session(database) as session:
                run = session.scalar(select(EngineRun))
                dispatch = session.scalar(select(CycleExecutionDispatch))
                assert run is not None and dispatch is not None
                assert run.thread_id == dispatch.thread_id == receipt["thread_id"]
                assert run.status == "completed" and run.completion_kind == "collection_teardown_v1"
                assert run.handle["extras"]["approved_artifact_sha256"] == checksum
            final = client.get(path).json()["attempt"]
            assert final["status"] == "completed" and final["engine_run_id"] == run.id
            results = client.get("/v1/cycles/cycle/results")
            assert results.status_code == 200, results.text
            assert len(results.json()["results"]) == 1
            assert results.json()["results"][0]["id"] == run.id
            assert results.json()["results"][0]["bindings_verified"] is True
            assert (
                client.post(
                    path, json={"expected_revision": 1, "idempotency_key": "launch"}
                ).status_code
                == 200
            )
            assert provider.start_count == provider.posts == provider.uploads == 1
            assert loopback.thread_creates == 1
    finally:
        event.remove(Session, "loaded_as_persistent", restore_timezone)
        database.dispose()
        MemoryArtifactStore.clear()


def test_ordinary_execution_still_requires_succeeded_script_generation():
    from apex.domain.pipeline import Phase
    from apex.graphs.pipeline.configurable import PipelineConfigurable

    assert "script_scenario" in (
        phase_subgraph._prerequisite_error({}, Phase.EXECUTION, PipelineConfigurable()) or ""
    )
    graph = builder.compile(checkpointer=InMemorySaver())
    with pytest.raises(ValueError, match="script_scenario.*neither earlier"):
        graph.invoke(
            {"title": "Unbound", "request": "Execute"},
            {"configurable": {"thread_id": "ordinary", "phases": ["execution"]}},
        )
