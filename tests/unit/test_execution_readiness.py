from datetime import UTC, datetime
from typing import Any, cast

import pytest
from fastapi import HTTPException

from apex.adapters.registry import ConnectionConfig, PortKind
from apex.auth.identity import ConsumerIdentity, ConsumerType, Role, ScopeRef
from apex.graphs.pipeline.configurable import PipelineConfigurable
from apex.routers.pipelines import ExecutionReadinessRequest, get_pipeline_execution_readiness
from apex.services import execution_readiness as readiness_module
from apex.services.connections import ConnectionResolver, StoredConnection


class Store:
    def __init__(self, entries: list[StoredConnection]):
        self.entries = entries
        self.calls: list[Any] = []

    async def get(self, connection_id: str):
        self.calls.append(connection_id)
        return next((row for row in self.entries if row.config.id == connection_id), None)

    async def find_default(self, kind: PortKind, project_id: str | None):
        self.calls.append((kind, project_id))
        return next((row for row in self.entries if row.config.kind == kind and row.enabled), None)


def persisted(kind: PortKind = PortKind.EXECUTION_ENGINE, **changes: Any) -> StoredConnection:
    values = {
        "config": ConnectionConfig(
            id="stored-connection",
            kind=kind,
            provider="sim" if kind == PortKind.EXECUTION_ENGINE else "stub",
            name="Fixture",
        ),
        "project_id": "p1",
        "enabled": True,
        "runtime_version": datetime.now(UTC),
        **changes,
    }
    return StoredConnection(**values)


@pytest.fixture
def resolver(monkeypatch):
    def install(entries: list[StoredConnection]):
        store = Store(entries)
        instance = ConnectionResolver(store=store)
        monkeypatch.setattr(readiness_module, "get_connection_resolver", lambda: instance)
        return store

    return install


@pytest.mark.parametrize("phases", [[], ["story_analysis"], ["reporting", "postmortem"]])
async def test_planning_only_never_resolves_any_connection(resolver, phases):
    store = resolver([])
    result = await readiness_module.execution_readiness(
        PipelineConfigurable.model_validate({"project_id": "p1", "phases": phases})
    )
    assert result.configuration_valid and not result.execution_selected
    assert store.calls == []


async def test_static_development_simulation_does_not_require_application(resolver):
    resolver([])
    result = await readiness_module.execution_readiness(PipelineConfigurable(project_id="p1"))
    assert result.configuration_valid and not result.requires_application


@pytest.mark.parametrize("kind", [PortKind.EXECUTION_ENGINE, PortKind.ARTIFACT_STORE])
@pytest.mark.parametrize("app_id", [None, "a1"])
async def test_persisted_engine_or_artifact_requires_application(resolver, kind, app_id):
    resolver([persisted(kind)])
    result = await readiness_module.execution_readiness(
        PipelineConfigurable(project_id="p1", app_id=app_id)
    )
    assert result.requires_application
    assert result.configuration_valid is bool(app_id)
    if not app_id:
        assert result.detail and "project and application" in result.detail


@pytest.mark.parametrize("problem", ["missing", "disabled", "cross-project", "version", "provider"])
async def test_unverified_selection_fails_closed_without_disclosing_connection(resolver, problem):
    row = persisted()
    if problem == "disabled":
        row = persisted(enabled=False)
    if problem == "cross-project":
        row = persisted(project_id="private-project")
    if problem == "version":
        row = persisted(runtime_version=None)
    resolver([] if problem == "missing" else [row])
    result = await readiness_module.execution_readiness(
        PipelineConfigurable(
            project_id="p1",
            app_id="a1",
            engine="apex_load" if problem == "provider" else "sim",
            connections={"execution_engine": "stored-connection"},
        )
    )
    assert not result.configuration_valid
    assert result.detail and "could not be verified" in result.detail
    assert "stored-connection" not in result.detail and "private-project" not in result.detail


async def test_readiness_scope_failure_happens_before_connection_lookup(resolver):
    store = resolver([])
    identity = ConsumerIdentity(
        consumer_id="c1",
        name="Viewer",
        consumer_type=ConsumerType.DASHBOARD,
        role=Role.VIEWER,
        scopes=[ScopeRef(project_id="p1")],
    )
    with pytest.raises(HTTPException) as error:
        await get_pipeline_execution_readiness(
            ExecutionReadinessRequest(project_id="p2", engine="sim", phases=["execution"]),
            identity,
            cast(Any, object()),
        )
    assert error.value.status_code == 403
    assert store.calls == []


async def test_readiness_uses_inferred_application_grant(resolver):
    resolver([persisted()])
    identity = ConsumerIdentity(
        consumer_id="c1",
        name="Viewer",
        consumer_type=ConsumerType.DASHBOARD,
        role=Role.VIEWER,
        scopes=[ScopeRef(project_id="p1", app_id="a1")],
    )
    result = await get_pipeline_execution_readiness(
        ExecutionReadinessRequest(project_id="p1", engine="sim", phases=["execution"]),
        identity,
        cast(Any, object()),
    )
    assert result.configuration_valid and result.requires_application


@pytest.mark.parametrize("phases", [["unknown"], ["execution", "execution"]])
def test_readiness_rejects_invalid_phase_contract(phases):
    with pytest.raises(ValueError):
        ExecutionReadinessRequest(engine="sim", phases=phases)


@pytest.mark.parametrize(
    "payload",
    [
        {"engine": "", "phases": []},
        {"engine": "sim", "phases": ["unknown"]},
        {"engine": "sim", "phases": [], "connection_id": "x" * 257},
        {"engine": "sim", "phases": [], "connection_id": "api_key=private-secret-value"},
    ],
)
def test_invalid_readiness_http_input_is_422_not_500(payload):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from apex.app.dependencies import get_current_identity
    from apex.persistence.db import get_session
    from apex.routers.pipelines import router

    app = FastAPI()
    app.include_router(router, prefix="/v1")
    app.dependency_overrides[get_current_identity] = lambda: ConsumerIdentity(
        consumer_id="c1",
        name="Viewer",
        consumer_type=ConsumerType.DASHBOARD,
        role=Role.VIEWER,
        scopes=[ScopeRef(project_id="p1")],
    )
    app.dependency_overrides[get_session] = lambda: object()
    with TestClient(app) as client:
        response = client.post("/v1/pipelines/execution-readiness", json=payload)
    assert response.status_code == 422
