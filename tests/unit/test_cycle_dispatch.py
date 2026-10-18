"""HTTP-to-durable dispatch reservations, recovery and exact execution boundaries."""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from apex.app.dependencies import get_current_identity
from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.domain.cycle_executions import PreflightCheck
from apex.persistence.db import get_session
from apex.persistence.models import (
    Base,
    CycleExecutionDispatch,
    CycleExecutionPreparation,
    EngineRun,
)
from apex.persistence.models import TestCycle as CycleRow
from apex.routers import cycle_dispatch as api
from apex.routers.cycles import enabled, find
from apex.routers.pipelines import get_pipeline_read_service


class AsyncFacade:
    def __init__(self, session):
        self.session = session

    async def scalar(self, query):
        return self.session.scalar(query)

    async def scalars(self, query):
        return self.session.scalars(query)

    async def execute(self, query):
        return self.session.execute(query)

    async def commit(self):
        self.session.commit()

    async def rollback(self):
        self.session.rollback()

    async def refresh(self, row):
        self.session.refresh(row)

    def add(self, row):
        self.session.add(row)

    def expire_all(self):
        self.session.expire_all()


class Pipeline:
    def __init__(self):
        self.calls = []
        self.uncertain = False
        self.view = {"thread_status": "busy", "values": {}}

    async def start_run(self, **kwargs):
        self.calls.append(kwargs)
        await kwargs["before_run_start"]("exact-thread")
        if self.uncertain:
            raise TimeoutError("accepted but acknowledgement lost")
        return {"thread_id": "exact-thread", "run_id": "exact-run"}

    async def get_pipeline(self, thread_id):
        assert thread_id == "exact-thread"
        return self.view


@pytest.fixture
def app(monkeypatch):
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    with engine.begin() as conn:
        conn.exec_driver_sql("ATTACH DATABASE ':memory:' AS apex")
        for name in (
            "test_cycles",
            "cycle_execution_preparations",
            "cycle_execution_dispatches",
            "engine_runs",
        ):
            Base.metadata.tables[f"apex.{name}"].create(conn)
    with Session(engine) as session:
        for cycle_id in ("cycle", "second-cycle"):
            session.add(
                CycleRow(
                    id=cycle_id,
                    project_id="project",
                    app_id="app",
                    environment_id="env",
                    name="Cycle",
                    mode="live",
                    owner="author",
                    revision=1,
                    independent_review=False,
                    versions=[],
                )
            )
        for prep, cycle in (("prep", "cycle"), ("prep2", "second-cycle")):
            session.add(
                CycleExecutionPreparation(
                    id=prep,
                    cycle_id=cycle,
                    idempotency_key=prep,
                    request_fingerprint="digest",
                    snapshot={},
                    created_by="author",
                )
            )
        session.commit()
    identity = ConsumerIdentity(
        consumer_id="current-operator",
        name="Operator",
        consumer_type=ConsumerType.DASHBOARD,
        role=Role.OPERATOR,
        scopes=[ScopeRef(project_id="project", app_id="app")],
    )
    pipeline = Pipeline()
    blockers = []

    async def context(session, actor, cycle_id, prep):
        cycle = await find(session, actor, cycle_id)
        return (
            cycle,
            SimpleNamespace(connection_id="engine"),
            SimpleNamespace(connection_id="store"),
            SimpleNamespace(base_url="https://example.com", version=2),
            list(blockers),
        )

    monkeypatch.setattr(api, "dispatch_context", context)
    application = FastAPI()
    application.include_router(api.router, prefix="/v1")
    application.dependency_overrides[enabled] = lambda: None
    application.dependency_overrides[get_current_identity] = lambda: identity
    application.dependency_overrides[get_pipeline_read_service] = lambda: pipeline

    def sessions():
        with Session(engine, expire_on_commit=False) as session:
            yield AsyncFacade(session)

    application.dependency_overrides[get_session] = sessions
    with TestClient(application) as client:
        yield client, engine, pipeline, identity, blockers
    engine.dispose()


PATH = "/v1/cycles/cycle/executions/prep/dispatch"


def post(client, key="launch", revision=1, path=PATH):
    return client.post(path, json={"expected_revision": revision, "idempotency_key": key})


def test_http_launch_records_current_actor_exact_config_and_replays_without_new_pipeline(app):
    client, engine, pipeline, _, _ = app
    response = post(client)
    assert response.status_code == 200, response.text
    receipt = response.json()
    assert receipt["revision"] == 2
    assert receipt["attempt"]["created_by"] == "current-operator"
    assert receipt["attempt"]["status"] == "submitted"
    assert receipt["attempt"]["thread_id"] == "exact-thread"
    assert receipt["attempt"]["request_key"] == "launch"
    assert not receipt["can_dispatch"] and not receipt["can_resume"]
    with Session(engine) as session:
        row = session.scalar(select(CycleExecutionDispatch))
        assert row is not None
        assert row.active_environment_id == "env"
        assert row.pipeline_config["connections"] == {
            "execution_engine": "engine",
            "artifact_store": "store",
        }
    assert post(client).json()["attempt"] == receipt["attempt"]
    assert len(pipeline.calls) == 1
    assert pipeline.calls[0]["phases"] == ["execution"]
    assert "load_test" not in pipeline.calls[0]["configurable"]
    assert post(client, key="different").status_code == 409


def test_ambiguous_launch_retains_environment_and_same_key_recovers_across_operator(app):
    client, engine, pipeline, identity, _ = app
    pipeline.uncertain = True
    first = post(client).json()
    assert first["attempt"]["status"] == "uncertain" and first["can_resume"]
    with Session(engine) as session:
        row = session.scalar(select(CycleExecutionDispatch))
        assert row is not None
        assert row.active_environment_id == "env"
    identity.consumer_id = "second-operator"
    pipeline.uncertain = False
    resumed = post(client, revision=999).json()
    assert resumed["attempt"]["id"] == first["attempt"]["id"]
    assert resumed["attempt"]["created_by"] == "current-operator"
    assert [c["principal_id"] for c in pipeline.calls] == ["current-operator"] * 2
    assert pipeline.calls[0]["idempotency_key"] == pipeline.calls[1]["idempotency_key"]


def test_another_cycle_cannot_dispatch_to_the_same_active_environment(app):
    client, _, pipeline, _, _ = app
    assert post(client).status_code == 200
    second = "/v1/cycles/second-cycle/executions/prep2/dispatch"
    preview = client.get(second).json()
    assert not preview["can_dispatch"]
    assert any(c["key"] == "environment_busy" for c in preview["checks"])
    assert post(client, path=second).status_code == 409
    assert len(pipeline.calls) == 1


def test_new_preparation_reconciles_prior_environment_owner_without_revisiting_it(app):
    client, engine, pipeline, _, _ = app
    assert post(client).status_code == 200
    pipeline.view = {"thread_status": "error", "values": {}}
    second = "/v1/cycles/second-cycle/executions/prep2/dispatch"
    assert client.get(second).json()["can_dispatch"]
    with Session(engine) as session:
        prior = session.scalar(select(CycleExecutionDispatch))
        assert prior is not None
        assert prior.status == "failed" and prior.active_environment_id is None


def test_definitive_validation_failure_with_empty_engine_reservation_releases_environment(app):
    client, engine, _, _, _ = app
    assert post(client).status_code == 200
    with Session(engine) as session:
        session.add(
            EngineRun(
                id="rejected",
                thread_id="exact-thread",
                attempt=1,
                engine="apex_load",
                project_id="project",
                app_id="app",
                status="failed",
                handle={},
                ownership_known=True,
                scope_ownership_known=True,
            )
        )
        session.commit()
    assert client.get(PATH).json()["attempt"]["status"] == "failed"
    with Session(engine) as session:
        row = session.scalar(select(CycleExecutionDispatch))
        assert row is not None
        assert row.active_environment_id is None


@pytest.mark.parametrize(
    "role,scopes,code",
    [
        (Role.VIEWER, [ScopeRef(project_id="project")], 403),
        (Role.OPERATOR, [], 404),
        (Role.OPERATOR, [ScopeRef(project_id="project", app_id="other")], 404),
    ],
)
def test_role_and_scope_refusals_happen_before_any_launch(app, role, scopes, code):
    client, _, pipeline, identity, _ = app
    identity.role, identity.scopes = role, scopes
    assert post(client).status_code == code
    assert pipeline.calls == []


def test_stale_revision_and_current_prerequisites_block_without_an_intent(app):
    client, engine, pipeline, _, blockers = app
    assert post(client, revision=9).status_code == 409
    blockers.append(
        PreflightCheck(
            key="approval", label="Approval", status="blocked", detail="Approval revoked"
        )
    )
    assert post(client).status_code == 409
    with Session(engine) as session:
        assert session.scalar(select(CycleExecutionDispatch)) is None
    assert pipeline.calls == []


def test_failed_before_engine_reservation_releases_environment_without_claiming_results(app):
    client, engine, pipeline, _, _ = app
    assert post(client).status_code == 200
    pipeline.view = {
        "thread_status": "error",
        "values": {"phase_results": {"execution": {"status": "failed"}}},
    }
    result = client.get(PATH).json()
    assert result["attempt"]["status"] == "failed"
    assert result["attempt"]["external_run_id"] is None
    with Session(engine) as session:
        row = session.scalar(select(CycleExecutionDispatch))
        assert row is not None
        assert row.active_environment_id is None


def test_terminal_engine_needs_confirmed_cleanup_before_environment_release(app):
    client, engine, _, _, _ = app
    assert post(client).status_code == 200
    with Session(engine) as session:
        session.add(
            EngineRun(
                id="engine-run",
                thread_id="exact-thread",
                attempt=1,
                engine="apex_load",
                project_id="project",
                app_id="app",
                status="failed",
                external_run_id="provider-run",
                handle={},
                ownership_known=True,
                scope_ownership_known=True,
            )
        )
        session.commit()
    assert client.get(PATH).json()["attempt"]["status"] == "cleanup_pending"
    with Session(engine) as session:
        row = session.get(EngineRun, "engine-run")
        assert row is not None
        row.completion_kind = "cleanup_teardown_v1"
        session.commit()
    result = client.get(PATH).json()["attempt"]
    assert result["status"] == "failed" and result["engine_run_id"] == "engine-run"
    with Session(engine) as session:
        row = session.scalar(select(CycleExecutionDispatch))
        assert row is not None
        assert row.active_environment_id is None


def test_unique_database_constraint_serializes_concurrent_environment_reservations(app):
    from sqlalchemy.exc import IntegrityError

    _, engine, _, _, _ = app
    with Session(engine) as session:
        for i, cycle, prep in ((1, "cycle", "prep"), (2, "second-cycle", "prep2")):
            session.add(
                CycleExecutionDispatch(
                    id=str(i),
                    cycle_id=cycle,
                    preparation_id=prep,
                    request_key=str(i),
                    active_environment_id="env",
                    status="reserved",
                    pipeline_config={},
                    created_by="operator",
                )
            )
        with pytest.raises(IntegrityError):
            session.commit()


@pytest.mark.asyncio
async def test_binding_rejects_reexecution_before_reading_any_durable_or_provider_state():
    from apex.graphs.pipeline.configurable import PipelineConfigurable
    from apex.services.cycle_dispatch import load_dispatch_binding

    with pytest.raises(ValueError, match="exactly one"):
        await load_dispatch_binding(PipelineConfigurable(cycle_dispatch_id="dispatch"), "thread", 2)


def test_viewer_can_read_receipt_with_explicit_launch_permission_reason(app):
    client, _, _, identity, _ = app
    assert post(client).status_code == 200
    identity.role = Role.VIEWER
    response = client.get(PATH)
    assert response.status_code == 200
    assert response.json()["attempt"]["created_by"] == "current-operator"
    assert not response.json()["can_resume"]
    assert any(
        check["key"] == "launch_permission" and check["status"] == "blocked"
        for check in response.json()["checks"]
    )


@pytest.mark.parametrize("second_key", ["launch", "different"])
def test_overlapping_http_requests_reconcile_one_durable_launch(app, second_key):
    import asyncio
    import threading
    from concurrent.futures import ThreadPoolExecutor

    client, engine, pipeline, _, _ = app
    started = threading.Event()
    release = threading.Event()
    original = pipeline.start_run
    logical_runs = {}

    async def overlapping(**kwargs):
        key = kwargs["idempotency_key"]
        if key in logical_runs:
            return logical_runs[key]
        logical_runs[key] = {"thread_id": "exact-thread", "run_id": "exact-run"}
        started.set()
        await asyncio.to_thread(release.wait, 5)
        return await original(**kwargs)

    pipeline.start_run = overlapping
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(post, client)
        assert started.wait(5)
        second = pool.submit(post, client, second_key)
        response = second.result(5)
        release.set()
        first_response = first.result(5)
    assert response.status_code == (200 if second_key == "launch" else 409)
    assert first_response.status_code == 200
    assert len(logical_runs) == 1
    with Session(engine) as session:
        rows = session.scalars(select(CycleExecutionDispatch)).all()
        assert len(rows) == 1
        assert rows[0].thread_id == "exact-thread"
        assert rows[0].status == "submitted"


@pytest.mark.parametrize("bind_first", [False, True])
def test_cancel_races_thread_binding_without_resurrecting_or_stopping_a_possible_run(
    app, bind_first
):
    import asyncio
    import threading
    from concurrent.futures import ThreadPoolExecutor

    client, engine, pipeline, _, blockers = app
    started, release = threading.Event(), threading.Event()
    native_creates = []

    async def delayed(**kwargs):
        if bind_first:
            await kwargs["before_run_start"]("exact-thread")
        started.set()
        await asyncio.to_thread(release.wait, 5)
        if not bind_first:
            await kwargs["before_run_start"]("exact-thread")
        native_creates.append("exact-run")
        return {"thread_id": "exact-thread", "run_id": "exact-run"}

    pipeline.start_run = delayed
    with ThreadPoolExecutor(max_workers=1) as pool:
        response = pool.submit(post, client)
        assert started.wait(5)
        blockers.append(
            PreflightCheck(
                key="revoked", label="Approval", status="blocked", detail="Approval revoked"
            )
        )
        preview = client.get(PATH).json()
        assert preview["can_cancel"] is (not bind_first)
        canceled = client.post(PATH + "/cancel", json={"dispatch_id": preview["attempt"]["id"]})
        assert canceled.status_code == (409 if bind_first else 200)
        release.set()
        assert response.result(5).status_code == 200
    final = client.get(PATH).json()["attempt"]
    assert final["status"] == ("submitted" if bind_first else "aborted")
    assert native_creates == (["exact-run"] if bind_first else [])
    if not bind_first:
        assert "No execution was started" in final["detail"]
        with Session(engine) as session:
            row = session.scalar(select(CycleExecutionDispatch))
            assert row is not None and row.active_environment_id is None


def test_cancel_requires_current_operator_scope_and_exact_intent(app):
    client, engine, _, identity, _ = app
    with Session(engine) as session:
        session.add(
            CycleExecutionDispatch(
                id="intent",
                cycle_id="cycle",
                preparation_id="prep",
                request_key="launch",
                active_environment_id="env",
                status="uncertain",
                pipeline_config={},
                created_by="old-operator",
            )
        )
        session.commit()
    assert client.post(PATH + "/cancel", json={"dispatch_id": "other"}).status_code == 409
    identity.role = Role.VIEWER
    assert client.post(PATH + "/cancel", json={"dispatch_id": "intent"}).status_code == 403
    identity.role = Role.OPERATOR
    identity.scopes = [ScopeRef(project_id="other")]
    assert client.post(PATH + "/cancel", json={"dispatch_id": "intent"}).status_code == 404
    identity.scopes = [ScopeRef(project_id="project", app_id="app")]
    identity.consumer_id = "second-operator"
    response = client.post(PATH + "/cancel", json={"dispatch_id": "intent"})
    assert response.status_code == 200
    assert response.json()["attempt"]["created_by"] == "old-operator"
    assert response.json()["attempt"]["canceled_by"] == "second-operator"
    with Session(engine) as session:
        row = session.get(CycleExecutionDispatch, "intent")
        assert row is not None and row.canceled_by == "second-operator"


@pytest.mark.parametrize("lost_response", [False, True])
def test_overlapping_http_requests_use_real_pipeline_idempotency_service(app, lost_response):
    import asyncio
    import threading
    from concurrent.futures import ThreadPoolExecutor

    import httpx

    from apex.services.pipeline_read import PipelineReadService
    from tests.unit.test_pipelines_create_run import AtomicClient

    client, engine, _, _, _ = app
    native = AtomicClient()
    pipeline = PipelineReadService(native)
    create_started, release, second_entered = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    original_create, original_start = native.runs.create, pipeline.start_run
    calls = 0

    async def create(*args, **kwargs):
        create_started.set()
        await asyncio.to_thread(release.wait, 5)
        result = await original_create(*args, **kwargs)
        if lost_response:
            raise httpx.ReadTimeout("accepted response lost")
        return result

    async def start(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            second_entered.set()
        return await original_start(**kwargs)

    native.runs.create = create
    pipeline.start_run = start
    client.app.dependency_overrides[get_pipeline_read_service] = lambda: pipeline
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(post, client)
        assert create_started.wait(5)
        second = pool.submit(post, client)
        assert second_entered.wait(5)
        release.set()
        assert first.result(5).status_code == 200
        assert second.result(5).status_code == 200
    assert native.runs.successful_creates == 1
    receipt = client.get(PATH).json()["attempt"]
    assert receipt["status"] == "submitted"
    with Session(engine) as session:
        rows = session.scalars(select(CycleExecutionDispatch)).all()
        assert len(rows) == 1
        assert rows[0].run_id == receipt["run_id"]
        assert rows[0].thread_id == receipt["thread_id"]


def test_changed_launch_checks_allow_only_thread_bound_same_intent_reconciliation(app):
    client, engine, pipeline, _, blockers = app
    pipeline.uncertain = True
    first = post(client).json()
    blockers.append(
        PreflightCheck(key="revoked", label="Approval", status="blocked", detail="Approval revoked")
    )
    preview = client.get(PATH).json()
    assert not preview["can_dispatch"] and preview["can_resume"]
    pipeline.uncertain = False
    resumed = post(client).json()
    assert resumed["attempt"]["id"] == first["attempt"]["id"]
    assert pipeline.calls[1]["configurable"] == pipeline.calls[0]["configurable"]
    assert pipeline.calls[1]["principal_id"] == pipeline.calls[0]["principal_id"]
    assert post(client, key="new-key").status_code == 409
    with Session(engine) as session:
        row = session.scalar(select(CycleExecutionDispatch))
        assert row is not None
        row.status = "uncertain"
        row.thread_id = None
        session.commit()
    preview = client.get(PATH).json()
    assert not preview["can_resume"] and preview["can_cancel"]
    assert post(client).status_code == 409


@pytest.mark.parametrize(
    "engine_status,dispatch_status",
    [
        ("ready", "submitted"),
        ("provisioning", "submitted"),
        ("running", "running"),
        ("collecting", "cleanup_pending"),
    ],
)
def test_provider_identity_does_not_claim_execution_started(app, engine_status, dispatch_status):
    client, engine, _, _, _ = app
    assert post(client).status_code == 200
    with Session(engine) as session:
        session.add(
            EngineRun(
                id="execution",
                thread_id="exact-thread",
                attempt=1,
                engine="apex_load",
                project_id="project",
                app_id="app",
                status=engine_status,
                external_run_id="provider-id",
                handle={"external_run_id": "provider-id"},
                ownership_known=True,
                scope_ownership_known=True,
            )
        )
        session.commit()
    result = client.get(PATH).json()["attempt"]
    assert result["status"] == dispatch_status
    with Session(engine) as session:
        row = session.scalar(select(CycleExecutionDispatch))
        assert row is not None and row.active_environment_id == "env"
